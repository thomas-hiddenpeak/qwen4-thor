#include "q4t/server/realtime_mcp_calls.h"

#include <algorithm>
#include <atomic>
#include <thread>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/realtime_mcp_item.h"
#include "q4t/server/realtime_shape.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
// Native helpers return a reply and an independently owned result snapshot.
constexpr size_t kResultReservation = 8ULL << 20;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
ObjectReply Capacity() {
  return Error(429, "realtime_mcp_call_capacity",
               "MCP call capacity exhausted");
}
ObjectReply Invalid(std::string_view message) {
  return Error(409, "realtime_mcp_call_owner_invalid", std::string(message));
}
bool Charge(size_t amount, size_t* remaining) {
  if (amount > *remaining) return false;
  *remaining -= amount;
  return true;
}
bool Charge(const Json& value, size_t* remaining) {
  size_t size = 0;
  return MeasureRealtimeStorage(value, *remaining, &size) &&
         Charge(size, remaining);
}
bool Charge(const RealtimeMcpCallJob& job, size_t* remaining) {
  if (!Charge(sizeof(job), remaining) ||
      !Charge(job.response_id.capacity(), remaining))
    return false;
  for (const auto* value : {&job.catalog.authority, &job.catalog.item,
                            &job.catalog.definition.declaration, &job.generated,
                            &job.native_call, &job.decision})
    if (!Charge(*value, remaining)) return false;
  const auto& definition = job.catalog.definition;
  const auto& endpoint = definition.credentials;
  for (const auto* value :
       {&definition.target_kind, &definition.target_id,
        &definition.target_binding, &endpoint.url, &endpoint.authorization,
        &endpoint.tunnel_id, &endpoint.tunnel_generation,
        &endpoint.tunnel_channel, &endpoint.provider_id})
    if (!Charge(value->capacity(), remaining)) return false;
  for (const auto& [key, value] : endpoint.headers)
    if (!Charge(
            sizeof(decltype(endpoint.headers)::value_type) + 4 * sizeof(void*),
            remaining) ||
        !Charge(key.capacity(), remaining) ||
        !Charge(value.capacity(), remaining))
      return false;
  return true;
}
bool Matches(const Json& generated, const Json& native) {
  for (const auto* field : {"id", "name", "server_label", "arguments"})
    if (generated.GetString(field) != native.GetString(field)) return false;
  return true;
}
}  // namespace

struct RealtimeMcpCalls::Call {
  RealtimeMcpCallJob job;
  RealtimeItemTicket ticket;
  std::optional<RealtimeItemTicket> approval;
  size_t output_index = 0, charge = 0;
  bool in_conversation = true, waiting = false, terminal = false;
  bool attempted = false, revoked = false, deleted = false;
  Clock::time_point deadline;
  std::thread worker;
  std::atomic<bool> stopped{false}, done{false};
  ObjectReply reply;
  RealtimeMcpExecution execution;
};
RealtimeMcpCalls::RealtimeMcpCalls(RealtimeConversation& history,
                                   RealtimeMcpCallCallbacks callbacks,
                                   RealtimeMcpCallLimits limits)
    : history_(history), callbacks_(std::move(callbacks)), limits_(limits) {
  valid_ = history.Valid() && callbacks_.run && callbacks_.emit &&
           callbacks_.new_id && limits_.workers && limits_.workers <= 4 &&
           limits_.entries && limits_.entries <= 64 && limits_.retained_bytes &&
           limits_.retained_bytes <= (64ULL << 20) &&
           limits_.timeout.count() > 0 && limits_.timeout.count() <= 30000 &&
           limits_.approval_timeout.count() > 0 &&
           limits_.approval_timeout.count() <= 300000;
}
RealtimeMcpCalls::~RealtimeMcpCalls() {
  Cancel();
  Join();
}
size_t RealtimeMcpCalls::Pending() const {
  return std::ranges::count_if(
      calls_, [](const auto& call) { return !call->terminal; });
}
size_t RealtimeMcpCalls::Workers() const {
  return std::ranges::count_if(
      calls_, [](const auto& call) { return call->worker.joinable(); });
}
bool RealtimeMcpCalls::UpdateCharge(Call& call) {
  const auto available = limits_.retained_bytes - retained_bytes_ + call.charge;
  auto remaining = available;
  if (!Charge(sizeof(Call), &remaining) || !Charge(call.job, &remaining) ||
      !Charge(call.ticket.owner.capacity(), &remaining) ||
      !Charge(call.ticket.id.capacity(), &remaining) ||
      (call.approval && (!Charge(call.approval->owner.capacity(), &remaining) ||
                         !Charge(call.approval->id.capacity(), &remaining))) ||
      (!call.terminal && !Charge(kResultReservation, &remaining)))
    return false;
  const auto charge = available - remaining;
  retained_bytes_ = retained_bytes_ - call.charge + charge;
  call.charge = charge;
  return true;
}
ObjectReply RealtimeMcpCalls::Event(std::string_view type, Json fields) {
  if (broken_ || cancelled_) return Invalid("MCP event owner closed");
  ObjectReply result;
  try {
    Set(&fields, "type", String(std::string(type)));
    Set(&fields, "event_id", String(callbacks_.new_id("event_")));
    result = callbacks_.emit(fields);
  } catch (...) {
    result =
        Error(503, "realtime_mcp_event_failed", "MCP event publication failed");
  }
  if (!Accepted(result)) {
    broken_ = true;
    Cancel();
  }
  return result;
}
ObjectReply RealtimeMcpCalls::Begin(const RealtimeItemTicket& ticket,
                                    const RealtimeMcpGeneratedCall& generated,
                                    const RealtimeMcpCatalog& catalog,
                                    std::string response_id,
                                    bool in_conversation) {
  if (!valid_ || cancelled_ || broken_) return Invalid("MCP call owner closed");
  if (calls_.size() == limits_.entries) return Capacity();
  if (!response_id.starts_with("resp_") || !ValidRequestId(response_id) ||
      generated.output_index >= 128 ||
      generated.item.GetString("id") != ticket.id || Owns(ticket))
    return Invalid("Invalid or repeated MCP generation handoff");
  auto references = Array({Object(
      {{"type", String("item_reference")}, {"id", String(ticket.id)}})});
  RealtimeConversationSnapshot pending;
  if (!history_.Capture(&references, &pending, true) ||
      pending.tickets != std::vector<RealtimeItemTicket>{ticket} ||
      pending.pending_outputs != std::vector<size_t>{0} ||
      pending.items.array[0].GetString("type") != "mcp_call" ||
      OutputJson(pending.items.array[0]) != OutputJson(generated.item))
    return Invalid("Handoff has no matching pending native history item");
  Json projection;
  bool loaded = false;
  ChatPlanError error;
  if (!ProjectRealtimeMcpCatalog(catalog.authority, &projection, &loaded,
                                 &error) ||
      !loaded || !catalog.loaded ||
      OutputJson(projection) != OutputJson(catalog.item) ||
      catalog.item.GetString("server_label") !=
          generated.item.GetString("server_label"))
    return Invalid("Handoff differs from its imported native catalog");
  const auto* tools = catalog.item.GetArray("tools");
  if (!tools || std::ranges::none_of(tools->array, [&](const auto& tool) {
        return tool.GetString("name") == generated.item.GetString("name");
      }))
    return Invalid("Generated tool is not in its imported catalog");
  auto call = std::make_unique<Call>();
  call->job.catalog = catalog;
  call->job.generated = generated.item;
  call->job.response_id = std::move(response_id);
  call->ticket = ticket;
  call->in_conversation = in_conversation;
  call->output_index = generated.output_index;
  call->deadline = Clock::now() + limits_.timeout;
  if (!UpdateCharge(*call)) return Capacity();
  calls_.push_back(std::move(call));
  return {};
}
ObjectReply RealtimeMcpCalls::Decide(const Json& supplied,
                                     std::optional<std::string_view> previous) {
  if (!valid_ || cancelled_ || broken_) return Invalid("MCP call owner closed");
  auto item = supplied;
  if (!item.Has("id")) Set(&item, "id", String(callbacks_.new_id("item_")));
  size_t bytes = 0;
  ChatPlanError error;
  if (!MeasureRealtimeStorage(item, 1ULL << 20, &bytes) ||
      !ValidateRealtimeShape(item, RealtimeShape::kItem, &error) ||
      item.GetString("type") != "mcp_approval_response")
    return Invalid("Invalid MCP approval response");
  const auto id = item.GetString("approval_request_id");
  for (auto& owned : calls_) {
    auto& call = *owned;
    if (!call.approval || call.approval->id != id) continue;
    Json approval;
    if (!call.waiting || call.terminal || call.revoked ||
        call.worker.joinable() || !history_.Get(*call.approval, &approval) ||
        Clock::now() >= call.deadline)
      return Invalid("MCP approval is not pending");
    auto old = std::move(call.job.decision);
    call.job.decision = item;
    if (!UpdateCharge(call)) {
      call.job.decision = std::move(old);
      return Capacity();
    }
    const auto predecessor =
        previous ? std::string(*previous) : history_.LastId();
    RealtimeItemTicket decision;
    auto status =
        history_.CreateMcpMetadata(item, item.GetString("id"), previous,
                                    &decision);
    if (!status) {
      call.job.decision = std::move(old);
      UpdateCharge(call);
      return Invalid(status.message());
    }
    call.waiting = false;
    call.job.action = RealtimeMcpAction::kDecide;
    call.deadline = Clock::now() + limits_.timeout;
    auto fields = Object({{"item", item}});
    if (!predecessor.empty())
      Set(&fields, "previous_item_id", String(predecessor));
    auto reply = Event("conversation.item.added", fields);
    return Accepted(reply) ? Event("conversation.item.done", std::move(fields))
                           : reply;
  }
  return Invalid("No owned MCP approval request");
}
void RealtimeMcpCalls::Start(Call& call) {
  if (call.job.action == RealtimeMcpAction::kExecute) {
    const auto emitted =
        Event("response.mcp_call.in_progress",
              Object({{"item_id", String(call.ticket.id)},
                      {"output_index", Number(call.output_index)}}));
    if (!Accepted(emitted)) return;
  }
  call.done = false;
  call.attempted = true;
  call.reply = {};
  call.execution = {};
  try {
    call.worker = std::thread([this, &call] {
      const bool cleanup = call.job.action == RealtimeMcpAction::kAbandon;
      const auto cancelled = [&] {
        return !cleanup && (call.stopped.load() || (callbacks_.interrupted &&
                                                    callbacks_.interrupted()));
      };
      try {
        call.reply =
            callbacks_.run(call.job, call.deadline, cancelled, &call.execution);
      } catch (...) {
        call.reply =
            Error(503, "realtime_mcp_execution_failed", "MCP worker failed");
      }
      call.done = true;
    });
  } catch (...) {
    call.reply =
        Error(503, "realtime_mcp_worker_unavailable", "MCP worker unavailable");
    call.done = true;
  }
}
void RealtimeMcpCalls::Abandon(Call& call) {
  call.revoked = true;
  call.stopped = true;
  call.waiting = false;
  call.job.action = RealtimeMcpAction::kAbandon;
  call.deadline = Clock::now() + std::chrono::seconds(2);
  call.reply = {};
  call.execution = {};
  call.done = false;
}
ObjectReply RealtimeMcpCalls::Complete(Call& call, const Json& native) {
  RealtimeMcpCallProjection projected;
  ChatPlanError error;
  if (!ProjectRealtimeMcpCall(native, &projected, &error) ||
      projected.phase == RealtimeMcpCallProjection::Phase::kPending ||
      !Matches(call.job.generated, projected.item))
    return Fail(
        call, "MCP outcome did not match the owned call; no automatic replay");
  Json current;
  const bool exists = history_.Get(call.ticket, &current).ok();
  if (exists) {
    const auto* approval = projected.item.Find("approval_request_id");
    if (current.GetString("approval_request_id").empty() && approval &&
        approval->IsString()) {
      // A cancelled proposal may have committed before its receipt reached
      // this session. The native cleanup has proved the exact original owner;
      // retain that approval identity without publishing another request.
      const auto request =
          Object({{"type", String("mcp_approval_request")},
                  {"id", *approval},
                  {"name", *projected.item.Find("name")},
                  {"server_label", *projected.item.Find("server_label")},
                  {"arguments", *projected.item.Find("arguments")}});
      if (!history_.BindMcpApproval(call.ticket, request) ||
          !history_.Get(call.ticket, &current))
        return Fail(call, "MCP outcome approval could not be retained");
    }
    if (!current.Has("approval_request_id") && approval && approval->IsNull())
      std::erase_if(projected.item.object, [](const auto& entry) {
        return entry.first == "approval_request_id";
      });
    if (current.GetString("approval_request_id") !=
        projected.item.GetString("approval_request_id"))
      return Fail(call, "MCP outcome approval differs; no automatic replay");
    const auto status =
        history_.Update(call.ticket, projected.item, true, true);
    if (!status)
      return Fail(call,
                  "MCP result exceeds available session history; durable "
                  "result retained");
  }
  call.terminal = true;
  call.waiting = false;
  ObjectReply reply;
  if (!call.deleted && exists && !cancelled_ && !broken_) {
    if (projected.phase == RealtimeMcpCallProjection::Phase::kFailed)
      reply = Event("response.mcp_call.failed",
                    Object({{"item_id", String(call.ticket.id)},
                            {"output_index", Number(call.output_index)}}));
    if (Accepted(reply))
      reply = Event("response.output_item.done",
                    Object({{"response_id", String(call.job.response_id)},
                            {"output_index", Number(call.output_index)},
                            {"item", projected.item}}));
    if (Accepted(reply) && call.in_conversation)
      reply =
          Event("conversation.item.done", Object({{"item", projected.item}}));
  }
  call.job.catalog = {};
  call.job.native_call = {};
  call.job.decision = {};
  call.reply = {};
  call.execution = {};
  UpdateCharge(call);
  return reply;
}
ObjectReply RealtimeMcpCalls::Fail(Call& call, std::string_view message) {
  auto item = call.job.generated;
  Json actual;
  if (history_.Get(call.ticket, &actual)) item = std::move(actual);
  Set(&item, "output", Json());
  Set(&item, "error",
      Object({{"type", String("protocol_error")},
              {"code", Number(-32603)},
              {"message", String(std::string(message))}}));
  const bool exists = history_.Get(call.ticket, &actual).ok();
  if (exists && !history_.Update(call.ticket, item, true, true))
    return Capacity();
  call.terminal = true;
  call.waiting = false;
  ObjectReply reply;
  if (!call.deleted && exists && !cancelled_ && !broken_) {
    reply = Event("response.mcp_call.failed",
                  Object({{"item_id", String(call.ticket.id)},
                          {"output_index", Number(call.output_index)}}));
    if (Accepted(reply))
      reply = Event("response.output_item.done",
                    Object({{"response_id", String(call.job.response_id)},
                            {"output_index", Number(call.output_index)},
                            {"item", item}}));
    if (Accepted(reply) && call.in_conversation)
      reply = Event("conversation.item.done", Object({{"item", item}}));
  }
  call.job.catalog = {};
  call.job.native_call = {};
  call.job.decision = {};
  call.reply = {};
  call.execution = {};
  UpdateCharge(call);
  return reply;
}
ObjectReply RealtimeMcpCalls::Finish(Call& call) {
  if (call.worker.joinable()) call.worker.join();
  const auto action = call.job.action;
  if (action == RealtimeMcpAction::kAbandon) {
    return Accepted(call.reply) ? Complete(call, call.reply.value)
                                : Fail(call,
                                       "MCP cleanup could not confirm a "
                                       "result; no automatic replay");
  }
  if (call.revoked || call.stopped.load()) {
    Abandon(call);
    return {};
  }
  if (!Accepted(call.reply)) {
    Abandon(call);
    return {};
  }
  if (action == RealtimeMcpAction::kPropose &&
      call.reply.value.GetString("type") == "mcp_approval_request") {
    const auto& approval = call.reply.value;
    ChatPlanError error;
    if (!ValidateRealtimeShape(approval, RealtimeShape::kItem, &error) ||
        approval.GetString("name") != call.job.generated.GetString("name") ||
        approval.GetString("server_label") !=
            call.job.generated.GetString("server_label") ||
        approval.GetString("arguments") !=
            call.job.generated.GetString("arguments")) {
      Abandon(call);
      return {};
    }
    RealtimeItemTicket ticket;
    const auto previous = history_.LastId();
    auto status =
        history_.CreateMcpMetadata(approval, approval.GetString("id"), {},
                                    &ticket);
    if (status) {
      status = history_.BindMcpApproval(call.ticket, approval);
      if (!status) history_.Erase(ticket);
    }
    if (!status) {
      Abandon(call);
      return {};
    }
    call.approval = ticket;
    Set(&call.job.generated, "approval_request_id", *approval.Find("id"));
    call.waiting = true;
    call.deadline = Clock::now() + limits_.approval_timeout;
    if (!UpdateCharge(call)) {
      Abandon(call);
      return {};
    }
    auto fields = Object({{"item", approval}});
    if (!previous.empty()) Set(&fields, "previous_item_id", String(previous));
    auto reply = Event("conversation.item.added", fields);
    if (Accepted(reply))
      reply = Event("conversation.item.done", std::move(fields));
    call.reply = {};
    return reply;
  }
  RealtimeMcpCallProjection projected;
  ChatPlanError error;
  if (!ProjectRealtimeMcpCall(call.reply.value, &projected, &error) ||
      !Matches(call.job.generated, projected.item) ||
      projected.item.GetString("approval_request_id") !=
          call.job.generated.GetString("approval_request_id")) {
    Abandon(call);
    return {};
  }
  const bool pending =
      projected.phase == RealtimeMcpCallProjection::Phase::kPending;
  if ((action == RealtimeMcpAction::kPropose && !pending) ||
      (action == RealtimeMcpAction::kDecide &&
       (call.job.decision.GetBool("approve")
            ? !pending
            : projected.phase != RealtimeMcpCallProjection::Phase::kFailed))) {
    Abandon(call);
    return {};
  }
  if (projected.phase != RealtimeMcpCallProjection::Phase::kPending)
    return Complete(call, call.reply.value);
  if (action == RealtimeMcpAction::kExecute) {
    Abandon(call);
    return {};
  }
  call.job.native_call = std::move(call.reply.value);
  call.reply = {};
  call.job.action = RealtimeMcpAction::kExecute;
  call.deadline = Clock::now() + limits_.timeout;
  if (!UpdateCharge(call)) Abandon(call);
  return {};
}
ObjectReply RealtimeMcpCalls::Tick() {
  if (!valid_ || cancelled_ || broken_) return Invalid("MCP call owner closed");
  if (callbacks_.interrupted && callbacks_.interrupted()) {
    Cancel();
    return Invalid("MCP call owner interrupted");
  }
  for (auto& owned : calls_) {
    auto& call = *owned;
    if (call.terminal) continue;
    RealtimeItemTicket current;
    if (!history_.Find(call.ticket.id, &current) || current != call.ticket ||
        (call.approval && (!history_.Find(call.approval->id, &current) ||
                           current != *call.approval))) {
      call.revoked = true;
      call.stopped = true;
    }
    if (call.done.load()) {
      call.done = false;
      auto reply = Finish(call);
      if (!Accepted(reply)) return reply;
    }
    if (call.terminal || call.worker.joinable()) continue;
    if (call.job.action != RealtimeMcpAction::kAbandon &&
        (call.revoked || call.stopped.load() ||
         Clock::now() >= call.deadline)) {
      if (call.attempted)
        Abandon(call);
      else {
        auto reply = Fail(call, "MCP call ended before native dispatch");
        if (!Accepted(reply)) return reply;
        continue;
      }
    }
    if (call.waiting || Workers() >= limits_.workers) continue;
    Start(call);
    if (broken_ || cancelled_) return Invalid("MCP publication stopped");
  }
  std::erase_if(calls_, [&](const auto& call) {
    if (!call->terminal || !call->deleted) return false;
    retained_bytes_ -= call->charge;
    return true;
  });
  return {};
}
bool RealtimeMcpCalls::Owns(const RealtimeItemTicket& ticket) const {
  return std::ranges::any_of(calls_, [&](const auto& call) {
    return call->ticket == ticket ||
           (call->approval && *call->approval == ticket);
  });
}
ObjectReply RealtimeMcpCalls::Erase(const RealtimeItemTicket& ticket) {
  for (auto it = calls_.begin(); it != calls_.end(); ++it) {
    auto& call = *it;
    if (call->ticket != ticket &&
        (!call->approval || *call->approval != ticket))
      continue;
    const auto status = history_.Erase(ticket);
    if (!status) return Invalid(status.message());
    if (call->terminal) {
      retained_bytes_ -= call->charge;
      calls_.erase(it);
      return {};
    }
    call->deleted |= call->ticket == ticket;
    if (!call->terminal) {
      call->revoked = true;
      call->stopped = true;
    }
    return {};
  }
  return Invalid("MCP history generation is not owned");
}
bool RealtimeMcpCalls::CancelResponse(std::string_view response_id) {
  bool found = false;
  for (auto& call : calls_) {
    if (call->terminal ||
        (response_id.empty() ? !call->in_conversation
                             : call->job.response_id != response_id))
      continue;
    call->revoked = true;
    call->stopped = true;
    found = true;
  }
  return found;
}
void RealtimeMcpCalls::Cancel() noexcept {
  cancelled_ = true;
  for (auto& call : calls_) {
    if (call->terminal) continue;
    call->revoked = true;
    call->stopped = true;
  }
}
void RealtimeMcpCalls::Join() noexcept {
  for (auto& call : calls_)
    if (call->worker.joinable()) call->worker.join();
  const auto cleanup_deadline = Clock::now() + std::chrono::seconds(2);
  for (auto& call : calls_) {
    if (!call->terminal && call->attempted) {
      call->job.action = RealtimeMcpAction::kAbandon;
      bool confirmed = false;
      try {
        std::thread cleanup([&] {
          try {
            RealtimeMcpExecution result;
            confirmed = Accepted(callbacks_.run(
                call->job, cleanup_deadline, [] { return false; }, &result));
          } catch (...) {
          }
        });
        cleanup.join();
      } catch (...) {
      }
      if (!confirmed) ++cleanup_failures_;
    }
  }
  calls_.clear();
  retained_bytes_ = 0;
}
}  // namespace q4t::server
