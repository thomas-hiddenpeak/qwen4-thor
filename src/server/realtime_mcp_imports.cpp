#include "q4t/server/realtime_mcp_imports.h"

#include <algorithm>
#include <atomic>
#include <thread>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
constexpr size_t kCatalogBytes = 4ULL << 20;
constexpr size_t kHistoryReservation = 8192;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
ObjectReply Capacity() {
  return Error(429, "realtime_mcp_import_capacity",
               "MCP import capacity exhausted");
}
ObjectReply Missing() {
  return Error(409, "realtime_mcp_catalog_unavailable",
               "No completed native catalog for this history generation");
}
bool SameRequest(const ResponsesMcpDefinition& a,
                 const ResponsesMcpDefinition& b) {
  // Deployment resolution may add a binding and native endpoint fields.
  // It cannot change the request's declared target, headers or token.
  return a.target_kind == b.target_kind && a.target_id == b.target_id &&
         OutputJson(a.declaration) == OutputJson(b.declaration) &&
         a.credentials.headers == b.credentials.headers &&
         a.credentials.authorization == b.credentials.authorization;
}
bool Spend(size_t bytes, size_t* remaining) {
  if (bytes > *remaining) return false;
  *remaining -= bytes;
  return true;
}
bool Charge(const Json& value, size_t* remaining) {
  size_t bytes = 0;
  return MeasureRealtimeStorage(value, *remaining, &bytes) &&
         Spend(bytes, remaining);
}
bool Charge(const ResponsesMcpDefinition& definition, size_t* remaining) {
  if (!Spend(sizeof(definition), remaining) ||
      !Charge(definition.declaration, remaining))
    return false;
  const auto& endpoint = definition.credentials;
  for (const auto* value :
       {&definition.target_kind, &definition.target_id,
        &definition.target_binding, &endpoint.url, &endpoint.authorization,
        &endpoint.tunnel_id, &endpoint.tunnel_generation,
        &endpoint.tunnel_channel, &endpoint.provider_id})
    if (!Spend(value->capacity(), remaining)) return false;
  for (const auto& [name, value] : endpoint.headers)
    if (!Spend(
            sizeof(decltype(endpoint.headers)::value_type) + 4 * sizeof(void*),
            remaining) ||
        !Spend(name.capacity(), remaining) ||
        !Spend(value.capacity(), remaining))
      return false;
  return true;
}
Json EmptyCatalog(const ResponsesMcpDefinition& definition,
                  const std::string& id) {
  return Object({{"id", String(id)},
                 {"type", String("mcp_list_tools")},
                 {"server_label", *definition.declaration.Find("server_label")},
                 {"tools", Array()}});
}
}  // namespace

struct RealtimeMcpImports::Import {
  ResponsesMcpDefinition definition;
  RealtimeItemTicket ticket;
  Json empty;
  std::string previous;
  Clock::time_point deadline;
  size_t base_charge = 0, charge = 0;
  std::thread worker;
  std::atomic<bool> cancelled{false}, done{false};
  bool started = false, terminal = false, retired = false;
  ObjectReply reply;
  RealtimeMcpCatalog result;
};

RealtimeMcpImports::RealtimeMcpImports(RealtimeConversation& history,
                                       RealtimeMcpImportCallbacks callbacks,
                                       RealtimeMcpImportLimits limits)
    : history_(history), callbacks_(std::move(callbacks)), limits_(limits) {
  valid_ = history_.Valid() && callbacks_.import && callbacks_.emit &&
           callbacks_.new_id && limits_.workers && limits_.workers <= 4 &&
           limits_.entries && limits_.entries <= 64 && limits_.retained_bytes &&
           limits_.retained_bytes <= (16ULL << 20) &&
           limits_.timeout.count() > 0 && limits_.timeout.count() <= 30000;
}
RealtimeMcpImports::~RealtimeMcpImports() {
  Cancel();
  Join();
}
size_t RealtimeMcpImports::Pending() const {
  return std::ranges::count_if(entries_, [](const auto& work) {
    return !work->terminal && !work->retired;
  });
}
size_t RealtimeMcpImports::Workers() const {
  return std::ranges::count_if(
      entries_, [](const auto& work) { return work->worker.joinable(); });
}
ObjectReply RealtimeMcpImports::Event(std::string_view type, Json fields) {
  if (broken_ || cancelled_)
    return Error(409, "realtime_mcp_import_closed", "MCP publication closed");
  ObjectReply reply;
  try {
    Set(&fields, "type", String(std::string(type)));
    Set(&fields, "event_id", String(callbacks_.new_id("event_")));
    reply = callbacks_.emit(fields);
  } catch (...) {
    reply = Error(503, "realtime_mcp_event_failed", "MCP publication failed");
  }
  if (!Accepted(reply)) {
    broken_ = true;
    Cancel();
  }
  return reply;
}
ObjectReply RealtimeMcpImports::Begin(const ResponsesMcpDefinition& definition,
                                      std::string id, bool in_conversation,
                                      RealtimeItemTicket* ticket) {
  if (!valid_ || !ticket || cancelled_ || broken_)
    return Error(409, "realtime_mcp_import_closed", "MCP import unavailable");
  if (entries_.size() == limits_.entries) return Capacity();
  if (!id.starts_with("mcpl_") || !ValidRequestId(id))
    return Error(400, "invalid_mcp_catalog", "Invalid MCP catalog identity");
  // Validate before copying, reserving, emitting or launching a worker.
  size_t remaining = limits_.retained_bytes - retained_bytes_;
  if (!Charge(definition, &remaining)) return Capacity();
  ResponsesMcpDefinition canonical;
  ChatPlanError error;
  if (!PlanResponsesMcpDefinition(definition.declaration, &canonical, &error))
    return Error(400, "invalid_mcp_definition",
                 "Invalid MCP import definition");
  BindResponsesMcpCredentials(&canonical, {definition});
  if (!SameRequest(canonical, definition))
    return Error(400, "invalid_mcp_definition", "MCP target identity differs");
  auto work = std::make_unique<Import>();
  work->definition = definition;
  work->empty = EmptyCatalog(definition, id);
  work->previous = in_conversation ? history_.LastId() : std::string();
  work->deadline = Clock::now() + limits_.timeout;
  remaining = limits_.retained_bytes - retained_bytes_;
  if (!Spend(sizeof(Import), &remaining) ||
      !Charge(work->definition, &remaining) ||
      !Charge(work->empty, &remaining) ||
      !Spend(work->previous.capacity() + 2 * id.capacity() + 1024, &remaining))
    return Capacity();
  work->base_charge = limits_.retained_bytes - retained_bytes_ - remaining;
  work->charge = work->base_charge;
  const auto status = history_.BeginOutput(work->empty, id, in_conversation,
                                           kHistoryReservation, &work->ticket);
  if (!status)
    return Error(429, "realtime_mcp_history_capacity", status.message());
  auto* active = work.get();
  const auto reserved = work->ticket;
  try {
    entries_.push_back(std::move(work));
  } catch (...) {
    history_.Erase(reserved);
    throw;
  }
  retained_bytes_ += active->charge;
  *ticket = active->ticket;
  auto fields = Object({{"item", active->empty}});
  if (!active->previous.empty())
    Set(&fields, "previous_item_id", String(active->previous));
  auto reply = Event("conversation.item.added", std::move(fields));
  if (Accepted(reply))
    reply =
        Event("mcp_list_tools.in_progress", Object({{"item_id", String(id)}}));
  return reply;
}
void RealtimeMcpImports::Start(Import& work) {
  work.started = true;
  if (Clock::now() >= work.deadline) {
    work.reply = Error(408, "request_timeout", "MCP import deadline expired");
    work.done = true;
    return;
  }
  auto* active = &work;
  try {
    work.worker = std::thread([this, active] {
      try {
        const auto cancelled = [this, active] {
          return active->cancelled.load() ||
                 (callbacks_.interrupted && callbacks_.interrupted());
        };
        if (cancelled()) {
          active->reply =
              Error(499, "request_cancelled", "MCP import cancelled");
        } else {
          active->reply =
              callbacks_.import(active->definition, active->ticket.id,
                                active->deadline, cancelled, &active->result);
        }
        // Public projection has its own bounded result. Never retain arbitrary
        // callback messages or a second copy of the transport payload.
        active->reply.value = Json();
        active->reply.message.clear();
        active->reply.message.shrink_to_fit();
      } catch (...) {
        active->reply = Error(503, "realtime_mcp_import_failed",
                              "Native catalog import failed");
      }
      active->done.store(true);
    });
  } catch (...) {
    work.reply = Error(503, "realtime_mcp_worker_unavailable",
                       "MCP import worker unavailable");
    work.done = true;
  }
}
ObjectReply RealtimeMcpImports::Finish(Import& work) {
  Json item;
  if (!history_.Get(work.ticket, &item)) {
    work.retired = true;
    return {};  // Deleted/reused generations never receive late events.
  }
  bool loaded = false;
  ChatPlanError error;
  auto& result = work.result;
  size_t bytes = 0;
  bool success =
      !work.cancelled.load() && Clock::now() < work.deadline &&
      !(callbacks_.interrupted && callbacks_.interrupted()) &&
      Accepted(work.reply) && SameRequest(work.definition, result.definition) &&
      ProjectRealtimeMcpCatalog(result.authority, &item, &loaded, &error) &&
      loaded && result.loaded && item.GetString("id") == work.ticket.id &&
      item.GetString("server_label") ==
          work.definition.declaration.GetString("server_label") &&
      MeasureRealtimeStorage(result.item, kCatalogBytes, &bytes) &&
      OutputJson(result.item) == OutputJson(item);
  size_t remaining = limits_.retained_bytes - retained_bytes_;
  if (success)
    success = Charge(result.definition, &remaining) &&
              Charge(result.authority, &remaining) &&
              Charge(result.item, &remaining);
  if (success) {
    success = history_.Update(work.ticket, item, true, true).ok();
    if (success) {
      const auto growth = limits_.retained_bytes - retained_bytes_ - remaining;
      work.charge += growth;
      retained_bytes_ += growth;
    }
  }
  if (!success) {
    result = {};
    item = work.empty;
    if (!history_.Update(work.ticket, item, true))
      return Error(500, "realtime_mcp_history_failed",
                   "Owned MCP import could not reach a terminal state");
  }
  work.terminal = true;
  auto fields = Object({{"item", std::move(item)}});
  if (!work.previous.empty())
    Set(&fields, "previous_item_id", String(work.previous));
  auto reply = Event("conversation.item.done", std::move(fields));
  if (Accepted(reply))
    reply =
        Event(success ? "mcp_list_tools.completed" : "mcp_list_tools.failed",
              Object({{"item_id", String(work.ticket.id)}}));
  return reply;
}
void RealtimeMcpImports::Drop(size_t index) {
  retained_bytes_ -= entries_[index]->charge;
  entries_.erase(entries_.begin() + index);
}
ObjectReply RealtimeMcpImports::Tick(bool start_workers) {
  if (!valid_ || cancelled_ || broken_)
    return Error(409, "realtime_mcp_import_closed", "MCP import unavailable");
  for (size_t index = 0; index < entries_.size();) {
    auto& work = *entries_[index];
    Json item;
    if (!history_.Get(work.ticket, &item)) {
      work.retired = true;
      work.cancelled = true;
    }
    if (!work.started && !work.retired && Clock::now() >= work.deadline)
      Start(work);  // Expired queue entries close even when every slot is busy.
    if (work.started && work.done.load()) {
      if (work.worker.joinable()) work.worker.join();
      if (!work.retired && !work.terminal) {
        auto reply = Finish(work);
        if (!Accepted(reply)) return reply;
      }
    }
    if (work.retired && !work.worker.joinable()) {
      Drop(index);
      continue;
    }
    ++index;
  }
  if (!start_workers) return {};
  size_t workers = Workers();
  for (auto& work : entries_) {
    if (work->started || work->retired || work->terminal) continue;
    if (workers == limits_.workers) break;
    Start(*work);
    if (work->worker.joinable()) ++workers;
  }
  return {};
}
bool RealtimeMcpImports::Owns(const RealtimeItemTicket& ticket) const {
  return std::ranges::any_of(entries_, [&](const auto& work) {
    return !work->retired && work->ticket == ticket;
  });
}
bool RealtimeMcpImports::Pending(const RealtimeItemTicket& ticket) const {
  return std::ranges::any_of(entries_, [&](const auto& work) {
    return !work->retired && !work->terminal && work->ticket == ticket;
  });
}
bool RealtimeMcpImports::Find(const ResponsesMcpDefinition& definition,
                              RealtimeItemTicket* ticket) const {
  if (!ticket || cancelled_ || broken_) return false;
  for (const auto& work : entries_) {
    Json retained;
    if (work->retired || !SameRequest(definition, work->definition) ||
        !history_.Get(work->ticket, &retained))
      continue;
    *ticket = work->ticket;
    return true;
  }
  return false;
}
ObjectReply RealtimeMcpImports::Get(const RealtimeItemTicket& ticket,
                                    RealtimeMcpCatalog* catalog) const {
  if (!catalog || cancelled_ || broken_) return Missing();
  for (const auto& work : entries_) {
    if (work->ticket != ticket || work->retired || !work->terminal ||
        !work->result.loaded)
      continue;
    Json retained;
    if (!history_.Get(ticket, &retained) ||
        OutputJson(retained) != OutputJson(work->result.item))
      return Missing();
    *catalog = work->result;
    return {};
  }
  return Missing();
}
ObjectReply RealtimeMcpImports::Revoke(const RealtimeItemTicket& ticket,
                                       bool erase) {
  for (size_t i = 0; i < entries_.size(); ++i) {
    auto& work = *entries_[i];
    if (work.retired || work.ticket != ticket) continue;
    if (erase && !history_.Erase(ticket)) return Missing();
    work.cancelled = true;
    ObjectReply reply;
    if (!erase && !work.terminal) {
      // Result is still worker-owned; Finish may inspect it only after join.
      if (!history_.Update(work.ticket, work.empty, true)) return Missing();
      work.terminal = true;
      reply = Event("conversation.item.done", Object({{"item", work.empty}}));
      if (Accepted(reply))
        reply = Event("mcp_list_tools.failed",
                      Object({{"item_id", String(ticket.id)}}));
    }
    work.retired = true;
    if (!work.worker.joinable()) Drop(i);
    return reply;
  }
  return Missing();
}
ObjectReply RealtimeMcpImports::Retire(const RealtimeItemTicket& ticket) {
  return Revoke(ticket, false);
}
ObjectReply RealtimeMcpImports::Erase(const RealtimeItemTicket& ticket) {
  return Revoke(ticket, true);
}
void RealtimeMcpImports::Cancel() noexcept {
  cancelled_ = true;
  for (const auto& work : entries_) work->cancelled = true;
}
void RealtimeMcpImports::Join() noexcept {
  Cancel();
  for (const auto& work : entries_) {
    if (work->worker.joinable()) work->worker.join();
    if (!work->terminal) {
      try {
        history_.Erase(work->ticket);
      } catch (...) {
      }
    }
  }
  entries_.clear();
  retained_bytes_ = 0;
}
}  // namespace q4t::server
