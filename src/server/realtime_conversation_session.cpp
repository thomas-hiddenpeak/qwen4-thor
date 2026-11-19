#include "q4t/server/realtime_conversation_session.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/realtime_shape.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
constexpr size_t kResponses = 4, kDeltaCount = 256;
constexpr size_t kDeltaBytes = 8ULL << 20;
constexpr size_t kEventCount = 1024, kEventBytes = 32ULL << 20;
ObjectReply Invalid(std::string code, std::string message,
                    std::string param = {}) {
  auto reply = object_detail::Error(400, std::move(code), std::move(message));
  reply.value = Object();
  if (!param.empty()) Set(&reply.value, "param", String(std::move(param)));
  return reply;
}
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
ObjectReply Shape(const Json& event, RealtimeShape kind) {
  ChatPlanError error;
  const auto status = ValidateRealtimeWireShape(event, kind, &error);
  return status ? ObjectReply{}
                : Invalid(error.code, status.message(), error.param);
}
void MergeAudio(Json* current, const Json& update) {
  if (!current->IsObject() || !update.IsObject() ||
      (update.Has("type") &&
       current->GetString("type") != update.GetString("type"))) {
    *current = update;
    return;
  }
  for (const auto& [key, value] : update.object) {
    auto* old = current->Has(key) ? &Field(*current, key) : nullptr;
    if (old && old->IsObject() && value.IsObject())
      MergeAudio(old, value);
    else
      Set(current, key, value);
  }
}
}  // namespace

struct RealtimeConversationSession::Work {
  RealtimeConversationJob job;
  std::shared_ptr<RequestControl> control;
  std::unique_ptr<RealtimeResponseOutput> output;
  std::map<std::string, RealtimeItemTicket> tickets;
  std::unordered_set<std::string> deleted;
  std::thread worker;
  std::atomic<bool> done{false};
  RealtimeGenerationResult result;
  ObjectReply local_error;
  struct Queued {
    ResponseDelta delta;
    size_t charge = 0;
  };
  std::mutex mutex;
  std::condition_variable changed;
  std::deque<Queued> deltas;
  size_t bytes = 0, count = 0;

  ObjectReply Push(const ResponseDelta& input) {
    ResponseDelta copy = input;
    size_t charge = sizeof(Queued);
    for (const auto* value :
         {&copy.text, &copy.call.id, &copy.call.name, &copy.call.arguments}) {
      if (value->capacity() > kDeltaBytes - charge)
        return object_detail::Error(503, "realtime_delta_too_large",
                                    "Delta exceeds its storage limit");
      charge += value->capacity();
    }
    const auto deadline =
        std::min(control->deadline(), Clock::now() + std::chrono::seconds(15));
    std::unique_lock lock(mutex);
    while (count == kDeltaCount || charge > kDeltaBytes - bytes) {
      if (control->Cancelled() || Clock::now() >= deadline)
        return object_detail::Error(503, "realtime_output_backpressure",
                                    "Response output was not consumed");
      changed.wait_for(lock, std::chrono::milliseconds(5));
    }
    if (control->Cancelled())
      return object_detail::Error(409, control->Reason(), "Response cancelled");
    deltas.push_back({std::move(copy), charge});
    bytes += charge;
    ++count;
    return {};
  }
};

RealtimeConversationSession::RealtimeConversationSession(
    std::string id, std::string conversation_id, Json settings,
    WebSocketOutput& output, RealtimeConversationCallbacks callbacks,
    size_t message_bytes)
    : id_(std::move(id)),
      conversation_id_(std::move(conversation_id)),
      settings_(std::move(settings)),
      output_(output),
      callbacks_(std::move(callbacks)),
      message_bytes_(message_bytes),
      history_(id_) {}
RealtimeConversationSession::~RealtimeConversationSession() {
  Cancel();
  Join();
}
void RealtimeConversationSession::Release(Work& work) noexcept {
  if (!work.control) return;
  try {
    callbacks_.release(work.control);
  } catch (...) {
  }
  work.control.reset();
}
void RealtimeConversationSession::Cancel() noexcept {
  cancelled_ = true;
  for (auto& work : work_) {
    if (work->control)
      work->control->Cancel(RequestControl::State::kDisconnect);
    work->changed.notify_all();
  }
}
void RealtimeConversationSession::Join() noexcept {
  for (auto& work : work_) {
    if (work->worker.joinable()) work->worker.join();
    Release(*work);
  }
  work_.clear();
}
ObjectReply RealtimeConversationSession::Emit(Json event) {
  if (cancelled_ || output_.Closed() ||
      (callbacks_.interrupted && callbacks_.interrupted()))
    return object_detail::Error(409, "realtime_connection_closed",
                                "Realtime connection closed");
  auto text = OutputJson(event);
  if (text.size() > output_.MessageLimit())
    return object_detail::Error(503, "realtime_event_too_large",
                                "Output event exceeds its byte limit");
  if (events_.empty() && output_.TryPush(WebSocketOpcode::kText, text))
    return {};
  if (events_.size() == kEventCount ||
      text.capacity() > kEventBytes - event_bytes_)
    return object_detail::Error(503, "realtime_event_capacity_exhausted",
                                "Output event capacity exhausted");
  if (events_.empty())
    event_deadline_ = Clock::now() + std::chrono::seconds(15);
  events_.push_back(std::move(text));
  event_bytes_ += events_.back().capacity();
  return {};
}
bool RealtimeConversationSession::Flush() {
  while (!events_.empty()) {
    if (output_.Closed() || Clock::now() >= event_deadline_) return false;
    if (!output_.TryPush(WebSocketOpcode::kText, events_.front())) return true;
    event_bytes_ -= events_.front().capacity();
    events_.pop_front();
  }
  return true;
}
bool RealtimeConversationSession::Event(std::string_view type, Json fields) {
  Set(&fields, "type", String(std::string(type)));
  Set(&fields, "event_id", String(detail::NewRequestId("event_")));
  return Accepted(Emit(std::move(fields)));
}
bool RealtimeConversationSession::ErrorEvent(ObjectReply reply,
                                             std::string_view event_id) {
  if (reply.code.empty())
    reply = object_detail::Error(500, "realtime_callback_invalid",
                                 "Native callback did not confirm success");
  auto error = Object(
      {{"type", String(reply.http_status >= 500 ? "server_error"
                                                : "invalid_request_error")},
       {"code", String(std::string(reply.ClientCode()))},
       {"message", String(reply.ClientMessage())}});
  if (!event_id.empty()) Set(&error, "event_id", String(std::string(event_id)));
  if (const auto* param = reply.value.Find("param"))
    Set(&error, "param", *param);
  return Event("error", Object({{"error", std::move(error)}}));
}
bool RealtimeConversationSession::Start() {
  size_t storage = 0;
  if (started_ || cancelled_ || !history_.Valid() || !output_.Valid() ||
      !ValidRequestId(conversation_id_) || !callbacks_.validate ||
      !callbacks_.prepare || !callbacks_.execute || !callbacks_.acquire ||
      !callbacks_.release || !message_bytes_ ||
      message_bytes_ > kWebSocketMessageLimit ||
      !MeasureRealtimeStorage(settings_, kWebSocketMessageLimit, &storage))
    return false;
  auto event =
      Object({{"type", String("session.update")}, {"session", settings_}});
  if (!Accepted(Shape(event, RealtimeShape::kClientEvent)) ||
      settings_.GetString("type") != "realtime" ||
      !Accepted(callbacks_.validate(settings_)))
    return false;
  started_ = true;
  return Event("session.created", Object({{"session", settings_}})) &&
         Event(
             "conversation.created",
             Object({{"conversation",
                      Object({{"id", String(conversation_id_)},
                              {"object", String("realtime.conversation")}})}}));
}
bool RealtimeConversationSession::Settings(const Json& update,
                                           std::string_view event_id) {
  if (update.GetString("type") != "realtime")
    return ErrorEvent(Invalid("realtime_session_type_invalid",
                              "Session type cannot change", "session.type"),
                      event_id);
  if (update.Has("model") &&
      update.GetString("model") != settings_.GetString("model"))
    return ErrorEvent(Invalid("realtime_model_immutable",
                              "Session model cannot change", "session.model"),
                      event_id);
  if (const auto* tracing = settings_.Find("tracing");
      tracing && !tracing->IsNull() && update.Has("tracing") &&
      OutputJson(*tracing) != OutputJson(*update.Find("tracing")))
    return ErrorEvent(
        Invalid("realtime_tracing_immutable", "Enabled tracing cannot change",
                "session.tracing"),
        event_id);
  auto next = settings_;
  for (const auto& [key, value] : update.object) {
    if ((key == "audio" || key == "reasoning") && next.Has(key))
      MergeAudio(&Field(next, key), value);
    else
      Set(&next, key, value);
  }
  size_t storage = 0;
  if (!MeasureRealtimeStorage(next, kWebSocketMessageLimit, &storage))
    return ErrorEvent(
        Invalid("realtime_settings_too_large",
                "Session settings exceed their storage limit", "session"),
        event_id);
  auto reply =
      Shape(Object({{"type", String("session.update")}, {"session", next}}),
            RealtimeShape::kClientEvent);
  if (Accepted(reply)) reply = callbacks_.validate(next);
  if (!Accepted(reply)) return ErrorEvent(std::move(reply), event_id);
  settings_ = std::move(next);
  return Event("session.updated", Object({{"session", settings_}}));
}
bool RealtimeConversationSession::CreateItem(const Json& event) {
  RealtimeItemTicket ticket;
  const auto* previous = event.Find("previous_item_id");
  const auto predecessor = previous ? previous->str : history_.LastId();
  const auto status = history_.Create(
      *event.Find("item"), detail::NewRequestId("item_"),
      previous ? std::optional<std::string_view>(previous->str) : std::nullopt,
      &ticket);
  if (!status)
    return ErrorEvent(
        Invalid("realtime_item_invalid", status.message(), "item"),
        event.GetString("event_id"));
  Json item;
  if (!history_.Get(ticket, &item)) return false;
  auto fields = Object({{"item", std::move(item)}});
  if (!predecessor.empty() && predecessor != "root")
    Set(&fields, "previous_item_id", String(predecessor));
  return Event("conversation.item.added", fields) &&
         Event("conversation.item.done", std::move(fields));
}
bool RealtimeConversationSession::ItemCommand(const Json& event, bool erase) {
  const auto id = event.GetString("item_id");
  RealtimeItemTicket ticket;
  auto status = history_.Find(id, &ticket);
  Json item;
  if (status)
    status = erase ? history_.Erase(ticket) : history_.Get(ticket, &item);
  if (!status)
    return ErrorEvent(
        Invalid("realtime_item_not_found", status.message(), "item_id"),
        event.GetString("event_id"));
  if (!erase)
    return Event("conversation.item.retrieved",
                 Object({{"item", std::move(item)}}));
  for (auto& work : work_)
    if (work->tickets.contains(id)) work->deleted.insert(id);
  return Event("conversation.item.deleted", Object({{"item_id", String(id)}}));
}
ObjectReply RealtimeConversationSession::Store(Work& work, const Json& item,
                                               bool begin, bool terminal) {
  const auto id = item.GetString("id");
  const auto previous = history_.LastId();
  if (work.deleted.contains(id)) {
    ObjectReply result;
    result.value = Object({{"deleted", Boolean(true)}});
    return result;  // Neither retained history nor later events resurrect it.
  }
  Status status;
  if (begin) {
    RealtimeItemTicket ticket;
    status =
        history_.BeginOutput(item, id, work.job.in_conversation, 8192, &ticket);
    if (status) work.tickets.emplace(id, std::move(ticket));
  } else {
    const auto found = work.tickets.find(id);
    if (found == work.tickets.end())
      return object_detail::Error(500, "realtime_item_owner_missing",
                                  "Output owner missing");
    status = history_.Update(found->second, item, terminal, true);
  }
  if (!status)
    return object_detail::Error(503, "realtime_history_capacity_exhausted",
                                status.message());
  ObjectReply result;
  result.value = Object();
  if (!previous.empty())
    Set(&result.value, "previous_item_id", String(previous));
  return result;
}
bool RealtimeConversationSession::CreateResponse(const Json& event) {
  const auto event_id = event.GetString("event_id");
  const auto* parameters = event.Find("response");
  const Json response = parameters ? *parameters : Object();
  if (const auto* metadata = response.Find("metadata");
      metadata && !RealtimeMetadataBounded(*metadata))
    return ErrorEvent(Invalid("invalid_metadata", "Invalid response metadata",
                              "response.metadata"),
                      event_id);
  const auto conversation = response.GetString("conversation", "auto");
  if (conversation != "auto" && conversation != "none" &&
      conversation != conversation_id_)
    return ErrorEvent(
        Invalid("realtime_conversation_not_found",
                "Conversation is not in this session", "response.conversation"),
        event_id);
  const bool in_conversation = conversation != "none";
  if (work_.size() == kResponses ||
      (in_conversation && std::ranges::any_of(work_, [](const auto& work) {
         return work->job.in_conversation;
       })))
    return ErrorEvent(
        object_detail::Error(429, "realtime_response_capacity_exhausted",
                             "Active response capacity exhausted"),
        event_id);
  auto work = std::make_unique<Work>();
  ScopeExit unpublished([&] {
    if (work) Release(*work);
  });
  work->job = {
      detail::NewRequestId("resp_"), settings_, response, {}, in_conversation};
  auto status = history_.Snapshot(response.Find("input"), &work->job.snapshot);
  if (!status)
    return ErrorEvent(
        Invalid("realtime_input_invalid", status.message(), "response.input"),
        event_id);
  auto reply = callbacks_.prepare(work->job);
  if (!Accepted(reply)) return ErrorEvent(std::move(reply), event_id);
  work->control = callbacks_.acquire(work->job.response_id);
  if (work->control && work->control->id() != work->job.response_id) {
    // A mismatched callback result grants no ownership of another request.
    work->control.reset();
    return ErrorEvent(
        object_detail::Error(500, "realtime_request_owner_invalid",
                             "Response identity changed"),
        event_id);
  }
  if (!work->control || work->control->Cancelled()) {
    Release(*work);
    return ErrorEvent(object_detail::Error(503, "realtime_request_unavailable",
                                           "Response owner unavailable"),
                      event_id);
  }
  auto* active = work.get();
  RealtimeResponseOutputOptions options;
  options.response_id = work->job.response_id;
  options.conversation_id = conversation_id_;
  options.in_conversation = in_conversation;
  if (const auto* metadata = response.Find("metadata"))
    options.metadata = *metadata;
  work->output = std::make_unique<RealtimeResponseOutput>(
      std::move(options), [this](const Json& value) { return Emit(value); },
      [this, active](const Json& item, bool begin, bool terminal) {
        return Store(*active, item, begin, terminal);
      },
      [](std::string_view prefix) {
        return detail::NewRequestId(std::string(prefix));
      });
  // Establish ownership before publishing or spawning, including exception
  // paths.
  work_.push_back(std::move(work));
  if (!Accepted(active->output->Begin())) return false;
  try {
    active->worker = std::thread([this, active] {
      try {
        active->result = callbacks_.execute(
            active->job, active->control, [active](const ResponseDelta& delta) {
              return active->Push(delta);
            });
      } catch (...) {
        active->result.reply = object_detail::Error(
            500, "realtime_execution_failed", "Native generation failed");
      }
      active->done.store(true);
    });
  } catch (...) {
    active->result.reply = object_detail::Error(
        503, "realtime_worker_unavailable", "Response worker unavailable");
    active->done.store(true);
  }
  return true;
}
bool RealtimeConversationSession::CancelResponse(const Json& event) {
  const auto id = event.GetString("response_id");
  for (auto& work : work_) {
    if (id.empty() ? !work->job.in_conversation : id != work->job.response_id)
      continue;
    if (!work->control->Cancel()) break;
    work->changed.notify_all();
    return true;
  }
  return ErrorEvent(Invalid("realtime_response_not_active",
                            "Response is not active", "response_id"),
                    event.GetString("event_id"));
}
bool RealtimeConversationSession::Message(WebSocketOpcode opcode,
                                          std::string_view message) {
  if (!started_ || cancelled_ || opcode != WebSocketOpcode::kText ||
      message.size() > message_bytes_)
    return false;
  Json event;
  if (!io::ParseJson(std::string(message), &event, true,
                     {64, 65536, message_bytes_, true}) ||
      !event.IsObject())
    return ErrorEvent(Invalid("invalid_json", "Invalid Realtime JSON event"));
  const auto event_id = event.GetString("event_id");
  if (event_id.size() > 512)
    return ErrorEvent(Invalid("invalid_event_id",
                              "Event identity exceeds its limit", "event_id"));
  const auto shape = Shape(event, RealtimeShape::kClientEvent);
  if (!Accepted(shape)) return ErrorEvent(shape, event_id);
  size_t storage = 0;
  if (!MeasureRealtimeStorage(event, kWebSocketMessageLimit, &storage))
    return ErrorEvent(
        Invalid("realtime_event_too_large", "Event exceeds its storage limit"),
        event_id);
  const auto type = event.GetString("type");
  if (type == "session.update")
    return Settings(*event.Find("session"), event_id);
  if (type == "conversation.item.create") return CreateItem(event);
  if (type == "conversation.item.delete") return ItemCommand(event, true);
  if (type == "conversation.item.retrieve") return ItemCommand(event, false);
  if (type == "response.create") return CreateResponse(event);
  if (type == "response.cancel") return CancelResponse(event);
  return ErrorEvent(
      object_detail::Error(503, "realtime_processor_unavailable",
                           "The required native processor is not connected"),
      event_id);
}
bool RealtimeConversationSession::Tick() {
  if (!started_ || cancelled_ || output_.Closed() ||
      (callbacks_.interrupted && callbacks_.interrupted()) || !Flush())
    return false;
  // Bound one iteration, and preserve control-command responsiveness under
  // slow output. Producers block on their own mailbox, not the socket reader.
  if (!events_.empty()) return true;
  for (auto it = work_.begin(); it != work_.end();) {
    auto& work = **it;
    for (size_t i = 0; i < 16; ++i) {
      Work::Queued queued;
      {
        std::lock_guard lock(work.mutex);
        if (work.deltas.empty()) break;
        queued = std::move(work.deltas.front());
        work.deltas.pop_front();
      }
      if (Accepted(work.local_error)) {
        work.local_error = work.output->Append(queued.delta);
        if (!Accepted(work.local_error))
          work.control->Cancel(RequestControl::State::kFailure);
      }
      {
        std::lock_guard lock(work.mutex);
        work.bytes -= queued.charge;
        --work.count;
      }
      work.changed.notify_one();
    }
    bool empty;
    {
      std::lock_guard lock(work.mutex);
      empty = work.count == 0;
    }
    if (!work.done.load() || !empty) {
      ++it;
      continue;
    }
    if (work.worker.joinable()) work.worker.join();
    RealtimeResponseEnd end;
    end.state = work.result.state;
    end.message = &work.result.message;
    end.reason = work.result.reason;
    end.usage = work.result.usage;
    const auto& error =
        Accepted(work.local_error) ? work.result.reply : work.local_error;
    if (!Accepted(error)) {
      end.state = RealtimeResponseEnd::State::kFailed;
      end.error_code =
          error.code.empty() ? "realtime_execution_failed" : error.code;
      end.error_message = error.ClientMessage();
    }
    if (work.control->Cancelled() && !work.control->Failed()) {
      if (std::string_view(work.control->Reason()) == "explicit_cancel") {
        end.state = RealtimeResponseEnd::State::kCancelled;
        end.reason = "client_cancelled";
      } else {
        end.state = RealtimeResponseEnd::State::kFailed;
        end.error_code = work.control->Reason();
        end.error_message = "Response execution interrupted";
      }
    }
    auto terminal = work.output->Finish(end);
    if (!Accepted(terminal) && !work.output->Terminal()) {
      end.state = RealtimeResponseEnd::State::kFailed;
      end.reason.clear();
      end.error_code = std::string(terminal.ClientCode());
      end.error_message = terminal.ClientMessage();
      // Local mismatch can still close honestly; a failed sink refuses this
      // call without replaying any already-published callback or event.
      terminal = work.output->Finish(end);
    }
    if (Accepted(terminal) && end.state != RealtimeResponseEnd::State::kFailed &&
        end.state != RealtimeResponseEnd::State::kCancelled &&
        work.job.in_conversation && work.job.snapshot.default_context &&
        work.result.retained_input_indices) {
      const auto truncated = history_.TruncateDefault(
          work.job.snapshot, *work.result.retained_input_indices);
      if (!truncated)
        terminal = object_detail::Error(500, "realtime_context_commit_failed",
                                         truncated.message());
    }
    Release(work);
    it = work_.erase(it);
    if (!Accepted(terminal)) return false;
  }
  return Flush();
}
}  // namespace q4t::server
