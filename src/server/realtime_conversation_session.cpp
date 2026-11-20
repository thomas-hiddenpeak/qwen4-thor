#include "q4t/server/realtime_conversation_session.h"

#include <algorithm>
#include <variant>

#include "q4t/io/base64.h"

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
}  // namespace

struct RealtimeConversationSession::Work {
  RealtimeConversationJob job;
  RealtimeSpeechOptions speech;
  std::shared_ptr<RequestControl> control;
  std::unique_ptr<RealtimeResponseOutput> output;
  std::map<std::string, RealtimeItemTicket> tickets;
  std::unordered_set<std::string> deleted;
  std::thread worker;
  std::atomic<bool> done{false};
  bool prepared = false;
  // Native generation may already be Finished while its output is queued.
  // The public response still belongs to this session until response.done.
  bool cancelled_after_generation = false;
  RealtimeGenerationResult result;
  ObjectReply local_error;
  struct Queued {
    std::variant<ResponseDelta, RealtimeSpeechEvent> delta;
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
    return Enqueue({std::move(copy), charge});
  }
  ObjectReply Push(const RealtimeSpeechEvent& input) {
    if (!speech.enabled ||
        (input.kind != RealtimeSpeechEvent::Kind::kChunk &&
         input.kind != RealtimeSpeechEvent::Kind::kDone) ||
        input.bytes.size() > RealtimeSpeechEncoder::kMaximumChunkBytes ||
        input.transcript.size() > 1ULL << 20 ||
        (input.kind == RealtimeSpeechEvent::Kind::kChunk
             ? input.bytes.empty() || !input.transcript.empty()
             : !input.bytes.empty() || input.transcript.empty()))
      return object_detail::Error(503, "realtime_audio_delta_invalid",
                                  "Invalid native audio fragment");
    auto copy = input;
    const auto charge =
        sizeof(Queued) + copy.bytes.capacity() + copy.transcript.capacity();
    if (charge > kDeltaBytes)
      return object_detail::Error(503, "realtime_audio_delta_too_large",
                                  "Audio fragment exceeds storage capacity");
    return Enqueue({std::move(copy), charge});
  }
  ObjectReply Enqueue(Queued queued) {
    const auto charge = queued.charge;
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
    deltas.push_back(std::move(queued));
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
      history_(id_),
      speech_(id_) {
  if (callbacks_.input) {
    RealtimeConversationInputCallbacks input;
    input.now = callbacks_.now;
    input.emit = [this](const Json& event) { return Emit(event); };
    input.ready = [this] { return InputReady(); };
    input.speech = [this](bool started, auto, auto, bool interrupt) {
      return InputSpeech(started, interrupt);
    };
    input.capture_response = [this](RealtimePromptSelection* selected) {
      *selected = settings_.Current();
      return ObjectReply{};
    };
    input.committed = [this](RealtimeInputCommit notice) {
      if (!notice.settings.RespondOnCommit(notice.automatic))
        return ObjectReply{};
      return CreateResponse(Object(), &notice)
                 ? ObjectReply{}
                 : object_detail::Error(503, "realtime_output_failed",
                                        "Automatic response output failed");
    };
    auto native = *callbacks_.input;
    const auto original = native.interrupted;
    native.interrupted = [this, original] {
      return (callbacks_.interrupted && callbacks_.interrupted()) ||
             (original && original());
    };
    input_ = std::make_unique<RealtimeConversationInput>(
        id_, history_, std::move(native), std::move(input),
        callbacks_.max_audio_seconds, message_bytes_);
  }
}
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
  if (input_) input_->Cancel();
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
  if (input_) input_->Join();
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
  const auto& settings = settings_.Current().settings;
  size_t storage = 0;
  if (started_ || cancelled_ || !history_.Valid() || !output_.Valid() ||
      !ValidRequestId(conversation_id_) ||
      (!callbacks_.validate && !callbacks_.validate_prompt) ||
      !callbacks_.prepare || !callbacks_.execute || !callbacks_.acquire ||
      !callbacks_.release || !message_bytes_ ||
      message_bytes_ > kWebSocketMessageLimit ||
      !MeasureRealtimeStorage(settings, kWebSocketMessageLimit, &storage))
    return false;
  auto event =
      Object({{"type", String("session.update")}, {"session", settings}});
  if (!Accepted(Shape(event, RealtimeShape::kClientEvent)) ||
      settings.GetString("type") != "realtime" ||
      !Accepted(ValidateSelection(settings_.Current())))
    return false;
  started_ = true;
  return Event("session.created", Object({{"session", settings}})) &&
         Event(
             "conversation.created",
             Object({{"conversation",
                      Object({{"id", String(conversation_id_)},
                              {"object", String("realtime.conversation")}})}}));
}
ObjectReply RealtimeConversationSession::ValidateSelection(
    const RealtimePromptSelection& selection) {
  RealtimeSpeechOptions speech;
  ChatPlanError error;
  auto status =
      PlanRealtimeSpeech(selection.settings, Object(), &speech, &error);
  if (!status) return Invalid(error.code, status.message(), error.param);
  auto reply = ValidateSpeech(speech);
  if (!Accepted(reply)) return reply;
  if (callbacks_.validate_prompt) {
    reply = callbacks_.validate_prompt(selection);
  } else if (selection.prompt) {
    return object_detail::Error(503, "realtime_prompt_owner_unavailable",
                                "Prompt execution owner unavailable");
  } else {
    reply = callbacks_.validate(selection.settings);
  }
  if (!Accepted(reply)) return reply;
  RealtimeDuplexInput input;
  reply = PlanRealtimeDuplexInput(selection.settings, &input);
  if (!Accepted(reply)) return reply;
  if (!input_)
    return input.enabled
               ? object_detail::Error(503, "realtime_input_unavailable",
                                      "Native input is not connected")
               : ObjectReply{};
  // Configuration is the final validating transaction. Response-only settings
  // never call this method. A failed optional processor does not prevent text
  // settings updates that leave that processor's effective settings alone.
  if (input_->Failed()) {
    RealtimeDuplexInput current;
    const auto planned =
        PlanRealtimeDuplexInput(settings_.Current().settings, &current);
    if (Accepted(planned) && input.enabled == current.enabled &&
        input.transcript_events == current.transcript_events &&
        input.create_response == current.create_response &&
        input.interrupt_response == current.interrupt_response &&
        input.idle_timeout_ms == current.idle_timeout_ms &&
        OutputJson(
            RealtimeTranscriptionSettingsJson(input.processing, false)) ==
            OutputJson(
                RealtimeTranscriptionSettingsJson(current.processing, false)))
      return {};
  }
  return input_->Configure(std::move(input));
}
ObjectReply RealtimeConversationSession::ValidateSpeech(
    const RealtimeSpeechOptions& speech) const {
  if (locked_voice_ && !SameRealtimeVoice(speech, *locked_voice_))
    return Invalid("realtime_voice_locked",
                   "Voice is locked after audio output", "audio.output.voice");
  for (const auto& work : work_) {
    if (!work->speech.enabled) continue;
    if (!SameRealtimeVoice(speech, work->speech))
      return Invalid("realtime_voice_locked",
                     "An audio response owns the voice", "audio.output.voice");
    if (speech.chat.speech.speed != work->speech.chat.speech.speed)
      return Invalid("realtime_speed_locked",
                     "Speed changes require an idle turn",
                     "audio.output.speed");
  }
  return {};
}
RealtimePromptResolver RealtimeConversationSession::PromptResolver() {
  return [this](const Json& reference, size_t budget, PromptSnapshot* out) {
    const auto cancelled = [this] {
      return cancelled_ || output_.Closed() ||
             (callbacks_.interrupted && callbacks_.interrupted());
    };
    const auto stopped = [] {
      return object_detail::Error(499, "request_cancelled",
                                  "Prompt lookup cancelled");
    };
    if (cancelled()) return stopped();
    if (!callbacks_.resolve_prompt)
      return object_detail::Error(503, "prompt_registry_unavailable",
                                  "Prompt lookup unavailable");
    auto reply = callbacks_.resolve_prompt(reference, budget, cancelled, out);
    return cancelled() ? stopped() : reply;
  };
}
bool RealtimeConversationSession::Settings(const Json& update,
                                           std::string_view event_id) {
  if (update.GetString("type") != "realtime")
    return ErrorEvent(Invalid("realtime_session_type_invalid",
                              "Session type cannot change", "session.type"),
                      event_id);
  auto reply = settings_.Update(
      update, PromptResolver(),
      [this](const auto& selection) { return ValidateSelection(selection); });
  if (!Accepted(reply)) return ErrorEvent(std::move(reply), event_id);
  return Event("session.updated",
               Object({{"session", settings_.Current().settings}}));
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
  if (status) {
    if (input_ && input_->Owns(ticket)) {
      const auto reply =
          erase ? input_->Erase(ticket) : input_->Retrieve(ticket, &item);
      if (!Accepted(reply))
        return ErrorEvent(reply, event.GetString("event_id"));
    } else {
      status = erase ? history_.Erase(ticket) : history_.Get(ticket, &item);
    }
  }
  if (!status)
    return ErrorEvent(
        Invalid("realtime_item_not_found", status.message(), "item_id"),
        event.GetString("event_id"));
  RealtimeSpeechView view;
  const bool audio = speech_.Get(ticket, &view).ok();
  if (!erase) {
    if (audio) {
      std::string encoded;
      if (!view.bytes.empty() &&
          !io::EncodeBase64(view.bytes, 4 * ((kRealtimeAudioBytes + 2) / 3),
                            &encoded))
        return ErrorEvent(
            object_detail::Error(503, "realtime_audio_retrieval_failed",
                                 "Audio encoding failed"),
            event.GetString("event_id"));
      Set(&Field(item, "content").array[0], "audio",
          String(std::move(encoded)));
    }
    return Event("conversation.item.retrieved",
                 Object({{"item", std::move(item)}}));
  }
  if (audio && !speech_.Erase(ticket)) return false;
  for (auto& work : work_)
    if (work->tickets.contains(id)) {
      work->deleted.insert(id);
      if (audio) {
        if (!Accepted(work->output->TruncateAudio(id))) return false;
        work->cancelled_after_generation |= !work->control->Cancel();
        work->changed.notify_all();
      }
    }
  return Event("conversation.item.deleted", Object({{"item_id", String(id)}}));
}
bool RealtimeConversationSession::TruncateItem(const Json& event) {
  const auto id = event.GetString("item_id");
  RealtimeItemTicket ticket;
  RealtimeSpeechView view;
  const auto* index = event.Find("content_index");
  if (!index || !IsInputInteger(*index) || index->number != 0 ||
      !history_.Find(id, &ticket) || !speech_.Get(ticket, &view))
    return ErrorEvent(
        Invalid("realtime_audio_item_not_found",
                "No owned audio content at this position", "item_id"),
        event.GetString("event_id"));
  const auto* end = event.Find("audio_end_ms");
  if (!end || !IsInputInteger(*end) || end->number < 0 || end->number > 600000)
    return ErrorEvent(Invalid("realtime_audio_truncate_invalid",
                              "Invalid played duration", "audio_end_ms"),
                      event.GetString("event_id"));
  auto status = speech_.Truncate(ticket, static_cast<uint64_t>(end->number));
  if (!status)
    return ErrorEvent(Invalid("realtime_audio_truncate_invalid",
                              status.message(), "audio_end_ms"),
                      event.GetString("event_id"));
  if (!history_.TruncateAudioTranscript(ticket)) return false;
  for (auto& work : work_) {
    if (!work->tickets.contains(id)) continue;
    if (!Accepted(work->output->TruncateAudio(id))) return false;
    work->cancelled_after_generation |= !work->control->Cancel();
    work->changed.notify_all();
  }
  return Event("conversation.item.truncated",
               Object({{"item_id", String(id)},
                       {"content_index", Number(0)},
                       {"audio_end_ms", *end}}));
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
    if (status && work.speech.enabled && item.GetString("type") == "message") {
      status = speech_.Begin(ticket, work.speech.format, kRealtimeAudioBytes);
      if (!status) history_.Erase(ticket);
    }
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
ObjectReply RealtimeConversationSession::StoreAudio(
    Work& work, std::string_view id, std::string_view bytes,
    RealtimeAudioCommit commit) {
  const auto found = work.tickets.find(std::string(id));
  if (found == work.tickets.end())
    return object_detail::Error(500, "realtime_audio_owner_missing",
                                "No matching waveform owner");
  if (work.deleted.contains(found->first)) return {};
  Status status;
  if (commit == RealtimeAudioCommit::kAppend) {
    status = speech_.Append(found->second, bytes);
    if (status && !locked_voice_) locked_voice_ = work.speech;
  } else if (commit == RealtimeAudioCommit::kComplete) {
    status = speech_.Complete(found->second);
  } else {
    status = speech_.Interrupt(found->second);
  }
  return status ? ObjectReply{}
                : object_detail::Error(503, "realtime_audio_history_failed",
                                       status.message());
}
bool RealtimeConversationSession::CreateResponse(
    const Json& event, RealtimeInputCommit* automatic) {
  const auto event_id = event.GetString("event_id");
  const auto* parameters = event.Find("response");
  Json response = parameters ? *parameters : Object();
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
      (!automatic && in_conversation &&
       std::ranges::any_of(
           work_, [](const auto& work) { return work->job.in_conversation; })))
    return ErrorEvent(
        object_detail::Error(429, "realtime_response_capacity_exhausted",
                             "Active response capacity exhausted"),
        event_id);
  RealtimePromptSelection selection;
  ObjectReply reply;
  if (automatic) {
    if (!Accepted(automatic->context_status))
      return ErrorEvent(automatic->context_status);
    if (!automatic->response)
      return ErrorEvent(
          object_detail::Error(500, "realtime_response_capture_missing",
                               "Automatic response settings owner missing"));
    selection = std::move(*automatic->response);
  } else {
    reply = settings_.Response(response, PromptResolver(), {}, &selection);
  }
  if (!Accepted(reply)) return ErrorEvent(std::move(reply), event_id);
  if (response.Has("prompt") && selection.prompt)
    Set(&response, "prompt", selection.prompt->reference);
  auto work = std::make_unique<Work>();
  ScopeExit unpublished([&] {
    if (work) Release(*work);
  });
  work->job = {detail::NewRequestId("resp_"),
               std::move(selection.settings),
               response,
               {},
               in_conversation,
               std::move(selection.prompt)};
  work->job.native_input = bool(input_);
  ChatPlanError speech_error;
  auto status = PlanRealtimeSpeech(work->job.settings, response, &work->speech,
                                   &speech_error);
  if (!status)
    return ErrorEvent(
        Invalid(speech_error.code, status.message(), speech_error.param),
        event_id);
  reply = ValidateSpeech(work->speech);
  if (!Accepted(reply)) return ErrorEvent(std::move(reply), event_id);
  if (automatic) {
    work->job.snapshot = std::move(automatic->context);
  } else {
    status = history_.Capture(response.Find("input"), &work->job.snapshot);
  }
  if (!status)
    return ErrorEvent(
        Invalid("realtime_input_invalid", status.message(), "response.input"),
        event_id);
  const bool pending = !work->job.snapshot.pending_inputs.empty() ||
                       !work->job.snapshot.pending_outputs.empty();
  if (pending) {
    // Validate response options now without presenting pending audio as text.
    // The resolved immutable input is validated before starting actual work.
    auto preview = work->job;
    preview.snapshot = {};
    preview.snapshot.items = Array();
    reply = callbacks_.prepare(preview);
  } else {
    reply = callbacks_.prepare(work->job);
    work->prepared = Accepted(reply);
  }
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
  if (work->speech.enabled) options.audio_format = work->speech.format;
  if (const auto* metadata = response.Find("metadata"))
    options.metadata = *metadata;
  work->output = std::make_unique<RealtimeResponseOutput>(
      std::move(options), [this](const Json& value) { return Emit(value); },
      [this, active](const Json& item, bool begin, bool terminal) {
        return Store(*active, item, begin, terminal);
      },
      [](std::string_view prefix) {
        return detail::NewRequestId(std::string(prefix));
      },
      [this, active](std::string_view id, std::string_view bytes,
                     RealtimeAudioCommit commit) {
        return StoreAudio(*active, id, bytes, commit);
      });
  // Establish ownership before publishing or spawning, including exception
  // paths.
  work_.push_back(std::move(work));
  if (!Accepted(active->output->Begin())) return false;
  if (input_ && active->job.in_conversation) input_->CancelIdle();
  AdvanceWork(*active);
  return true;
}
void RealtimeConversationSession::StartWork(Work& work) {
  auto* active = &work;
  try {
    active->worker = std::thread([this, active] {
      try {
        active->result = callbacks_.execute(
            active->job, active->control,
            [active](const ResponseDelta& delta) {
              return active->Push(delta);
            },
            [active](const RealtimeSpeechEvent& audio) {
              return active->Push(audio);
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
}
void RealtimeConversationSession::AdvanceWork(Work& work) {
  if (work.worker.joinable() || work.done.load()) return;
  if (work.control->Cancelled()) {
    work.done = true;  // No executor or model admission to cancel/replay.
    return;
  }
  bool ready = false;
  const auto status = history_.Resolve(&work.job.snapshot, &ready);
  if (!status) {
    work.local_error =
        Invalid("realtime_input_invalid", status.message(), "response.input");
    work.done = true;
    return;
  }
  if (!ready || (input_ && input_->Executing())) return;
  for (const auto& other : work_) {
    if (other.get() == &work) break;
    if (work.job.in_conversation && other->job.in_conversation) return;
  }
  if (input_ && std::ranges::any_of(work_, [&](const auto& other) {
        return other.get() != &work && other->worker.joinable();
      }))
    return;
  if (!work.prepared) {
    work.local_error = callbacks_.prepare(work.job);
    if (!Accepted(work.local_error)) {
      work.done = true;
      return;
    }
    work.prepared = true;
  }
  StartWork(work);
}
bool RealtimeConversationSession::InputReady() {
  if (std::ranges::any_of(
          work_, [](const auto& work) { return work->worker.joinable(); }))
    return false;
  // The input processor calls this after settling its previous job and before
  // starting the next one. Give an already-ready response its turn; pending
  // responses that still need ASR must never block their own input work.
  for (const auto& work : work_) {
    if (work->done.load() || work->control->Cancelled()) continue;
    bool ready = false;
    if (history_.Resolve(&work->job.snapshot, &ready) && ready) return false;
  }
  return true;
}
bool RealtimeConversationSession::InputSpeech(bool started, bool interrupt) {
  if (!started || !interrupt) return true;
  for (auto& work : work_) {
    if (!work->job.in_conversation) continue;
    work->cancelled_after_generation |= !work->control->Cancel();
    work->changed.notify_all();
  }
  return true;
}
bool RealtimeConversationSession::CancelResponse(const Json& event) {
  const auto id = event.GetString("response_id");
  bool matched = false;
  for (auto& work : work_) {
    if (id.empty() ? !work->job.in_conversation : id != work->job.response_id)
      continue;
    work->cancelled_after_generation |= !work->control->Cancel();
    work->changed.notify_all();
    matched = true;
    if (!id.empty()) break;
  }
  if (matched) return true;
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
  if (type == "conversation.item.truncate") return TruncateItem(event);
  if (type == "response.create") return CreateResponse(event);
  if (type == "response.cancel") return CancelResponse(event);
  if (input_ && (type == "input_audio_buffer.append" ||
                 type == "input_audio_buffer.commit" ||
                 type == "input_audio_buffer.clear"))
    return input_->Message(message);
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
    AdvanceWork(work);
    for (size_t i = 0; i < 16; ++i) {
      Work::Queued queued;
      {
        std::lock_guard lock(work.mutex);
        if (work.deltas.empty()) break;
        queued = std::move(work.deltas.front());
        work.deltas.pop_front();
      }
      if (Accepted(work.local_error)) {
        if (const auto* delta = std::get_if<ResponseDelta>(&queued.delta)) {
          work.local_error = work.output->Append(*delta);
        } else {
          const auto& audio = std::get<RealtimeSpeechEvent>(queued.delta);
          work.local_error =
              audio.kind == RealtimeSpeechEvent::Kind::kChunk
                  ? work.output->AppendAudio(audio.bytes)
                  : work.output->CompleteAudio(audio.receipt, audio.transcript);
        }
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
    if (work.cancelled_after_generation) {
      end.state = RealtimeResponseEnd::State::kCancelled;
      end.reason = "client_cancelled";
    } else if (work.control->Cancelled() && !work.control->Failed()) {
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
    if (Accepted(terminal) &&
        end.state != RealtimeResponseEnd::State::kFailed &&
        end.state != RealtimeResponseEnd::State::kCancelled &&
        work.job.in_conversation && work.job.snapshot.default_context &&
        work.result.retained_input_indices) {
      const auto truncated = history_.TruncateDefault(
          work.job.snapshot, *work.result.retained_input_indices);
      if (!truncated)
        terminal = object_detail::Error(500, "realtime_context_commit_failed",
                                        truncated.message());
    }
    if (Accepted(terminal) && input_ && work.job.in_conversation &&
        (end.state == RealtimeResponseEnd::State::kCompleted ||
         end.state == RealtimeResponseEnd::State::kIncomplete) &&
        std::ranges::none_of(work_, [&](const auto& other) {
          return other.get() != &work && other->job.in_conversation;
        })) {
      uint64_t playback = 0;
      for (const auto& [id, ticket] : work.tickets) {
        (void)id;
        RealtimeSpeechView view;
        if (!speech_.Get(ticket, &view)) continue;
        const size_t width =
            view.format.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
        playback += view.bytes.size() / width *
                    (kRealtimeTicksPerSecond / view.format.rate);
      }
      input_->ResponseFinished(playback);
    }
    Release(work);
    it = work_.erase(it);
    if (!Accepted(terminal)) return false;
  }
  // Finishes ASR and may enqueue automatic responses. It never mutates work_
  // during the response iteration above. Ready arbitration prevents a second
  // ASR from stealing admission from a now-runnable response.
  if (input_ && !input_->Tick()) return false;
  return Flush();
}
}  // namespace q4t::server
