#include "q4t/server/live_native_backend.h"
#include "q4t/server/live_responses_settings.h"

#include "object_protocol.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
ObjectReply Missing(std::string param, std::string message) {
  auto reply = Error(503, "live_processor_unavailable", std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}
ObjectReply Unavailable() {
  return Missing("session", "Native Live backend is not active");
}
ObjectReply Connected(const Json& configuration, bool responses, bool decisions,
                      bool recording) {
  const auto* delegation = configuration.Find("delegation");
  const auto* triggers = configuration.GetArray("triggers");
  if (!delegation ||
      (delegation->GetString("type") != "client" &&
       (!responses || delegation->GetString("type") != "responses") &&
       (!decisions || delegation->GetString("type") != "decisions")))
    return Missing("session.delegation",
                   "Requested native delegation processor is not connected");
  if (!triggers || (!triggers->array.empty() && !decisions))
    return Missing("session.triggers",
                   "Native Decisions trigger processor is not connected");
  if (configuration.GetBool("store") && !recording) {
    auto reply = Error(403, "live_recording_disabled",
                       "Live recording requires deployment permission and an "
                       "authenticated client");
    reply.value = Object({{"param", String("session.store")}});
    return reply;
  }
  const auto* delay = delegation->Find("acknowledgement_delay_ms");
  if (!delay || !IsInputInteger(*delay) || delay->number < 0 ||
      delay->number > INT32_MAX)
    return Error(400, "invalid_request",
                 "Invalid native acknowledgement delay");
  return {};
}
}  // namespace
Status MapLiveStartupHistory(const Json& input, Json* output,
                             ChatPlanError* error) {
  if (!output || !error)
    return Status::Fail("Invalid Live startup history owner");
  *error = {};
  if (!input.IsArray() || input.array.size() > 128) {
    *error = {"invalid_request", "session.input", 400};
    return Status::Fail("Invalid Live startup history array");
  }
  auto mapped = Array();
  for (const auto& original : input.array) {
    const auto* content = original.GetArray("content");
    if (!content || content->array.size() != 1 ||
        !content->array[0].Find("text") ||
        !content->array[0].Find("text")->IsString()) {
      *error = {"invalid_request", "session.input", 400};
      return Status::Fail("Live startup messages require one text part");
    }
    auto role = original.GetString("role");
    const auto kind = content->array[0].GetString("type");
    if ((role != "developer" && role != "user" && role != "assistant") ||
        (original.Has("type") && original.GetString("type") != "message") ||
        (!kind.empty() &&
         (role == "assistant" ? kind != "text" && kind != "output_text"
                              : kind != "input_text"))) {
      *error = {"invalid_request", "session.input", 400};
      return Status::Fail("Invalid Live startup message role or text type");
    }
    // Live has developer/user/assistant. The internal Realtime ledger names
    // the application instruction role system; retain its position and text.
    // This never promotes a user message or accepts a public Realtime role.
    if (role == "developer") role = "system";
    mapped.array.push_back(Object(
        {{"type", String("message")},
         {"role", String(role)},
         {"content",
          Array({Object({{"type", String(role == "assistant" ? "output_text"
                                                             : "input_text")},
                         {"text", *content->array[0].Find("text")}})})}}));
  }
  *output = std::move(mapped);
  return {};
}
Status PlanNativeLiveClient(const Json& configuration,
                            const ChatPlanConfig& planning,
                            NativeLivePlan* output, ChatPlanError* error,
                            bool responses_connected, bool decisions_connected,
                            bool recording_connected, LiveTransport transport) {
  if (!output || !error) return Status::Fail("Missing native Live plan owner");
  *error = {};
  auto status = ValidateLiveWireShape(configuration, LiveShape::kSessionStart,
                                      error, "session");
  if (!status) return status;
  const auto connected = Connected(configuration, responses_connected,
                                   decisions_connected, recording_connected);
  if (!Accepted(connected)) {
    *error = {connected.code, connected.value.GetString("param"),
              connected.http_status};
    return Status::Fail(connected.message);
  }
  NativeLivePlan next;
  if (decisions_connected) {
    status = CheckLiveDecisionSettings(configuration, planning, error);
    if (!status) return status;
    next.decision_catalog.request_deadline_ms = planning.request_deadline_ms;
    next.decision_catalog.model = planning.model;
    next.decision_catalog.model_aliases = planning.model_aliases;
  }
  next.settings =
      DefaultRealtimeChatSettings(planning, configuration.GetString("model"));
  const auto* instructions = configuration.Find("instructions");
  const auto* input = configuration.GetArray("input");
  const auto* audio = configuration.Find("audio");
  const auto* format = audio ? audio->Find("format") : nullptr;
  const auto* speech = audio ? audio->Find("output") : nullptr;
  const auto* voice = speech ? speech->Find("voice") : nullptr;
  if (!instructions || !instructions->IsString() || !input || !voice ||
      (transport == LiveTransport::kWebSocket && !format)) {
    *error = {"invalid_request", "session", 400};
    return Status::Fail("Native Live requires resolved startup configuration");
  }
  if (transport != LiveTransport::kWebSocket) {
    if (format) {
      *error = {"invalid_request", "session.audio.format", 400};
      return Status::Fail("This transport negotiates its media format");
    }
    // The media worker exchanges mono 24 kHz PCM for every negotiated RTP
    // codec.
    next.format = {RealtimeAudioEncoding::kPcm16, 24000};
  } else {
    const auto* rate = format->Find("rate");
    if (!rate || !IsInputInteger(*rate)) {
      *error = {"invalid_request", "session.audio.format.rate", 400};
      return Status::Fail("Invalid Live sample rate");
    }
    const auto type = format->GetString("type");
    if (type == "audio/pcm" && (rate->number == 16000 || rate->number == 24000))
      next.format = {RealtimeAudioEncoding::kPcm16, uint32_t(rate->number)};
    else if ((type == "audio/pcmu" || type == "audio/pcma") &&
             rate->number == 8000)
      next.format = {type == "audio/pcmu" ? RealtimeAudioEncoding::kMuLaw
                                          : RealtimeAudioEncoding::kALaw,
                     8000};
    else {
      *error = {"invalid_request", "session.audio.format", 400};
      return Status::Fail("Invalid Live audio format");
    }
  }
  Set(&next.settings, "instructions", *instructions);
  Set(&next.settings, "output_modalities", Array({String("audio")}));
  Set(&Field(Field(next.settings, "audio"), "output"), "voice", *voice);
  next.settings = LiveClientFrontendSettings(std::move(next.settings));
  status = MapLiveStartupHistory(*input, &next.input, error);
  if (!status) return status;
  RealtimeChatPlan preview;
  status = PlanRealtimeChat(next.settings, Object(), next.input, planning,
                            &preview, error, nullptr, true);
  if (!status) return status;
  *output = std::move(next);
  return {};
}
NativeLiveClientBackend::NativeLiveClientBackend(
    std::string id, LiveFrontendNative native,
    LiveClientContextCallbacks context, uint32_t max_audio_seconds,
    size_t message_bytes, LiveResponsesNative responses,
    LiveDecisionNative decisions,
    std::unique_ptr<LiveRecordingCapture> recording)
    : id_(std::move(id)),
      context_callbacks_(std::move(context)),
      responses_native_(std::move(responses)),
      decisions_native_(std::move(decisions)),
      recording_(std::move(recording)),
      message_bytes_(message_bytes),
      frontend_(id_, Bind(std::move(native)), max_audio_seconds,
                message_bytes) {}
NativeLiveClientBackend::~NativeLiveClientBackend() {
  Cancel();
  Join();
}
LiveFrontendNative NativeLiveClientBackend::Bind(LiveFrontendNative native) {
  if (recording_)
    native.record = [this, observer = std::move(native.record)](auto channel, auto ticks, auto bytes) {
      if (observer) observer(channel, ticks, bytes);
      recording_->Audio(channel, ticks, bytes);
    };
  if (decisions_native_.Bound() && native.emit) {
    native.emit = [this, emit = std::move(native.emit)](Json event) {
      const auto type = event.GetString("type");
      if (!decisions_ || (type != "session.input_transcript.delta" &&
                          type != "session.output_transcript.delta"))
        return emit(std::move(event));
      auto delta = event.GetString("delta");
      const auto begin = event.GetInt("start_ms");
      auto reply = emit(std::move(event));
      return Accepted(reply) ? decisions_->Transcript(type, std::move(delta),
                                                      reply.value.str, begin)
                             : reply;
    };
  }
  native.fixture_ready = [this] {
    return !responses_ || !responses_->Running();
  };
  native.completed = [this](const auto& job, const auto& result,
                            const auto& output) {
    return context_ ? context_->Complete(job, result, output) : Unavailable();
  };
  return native;
}
ObjectReply NativeLiveClientBackend::Prepare(Json configuration,
                                             NativeLivePlan plan) {
  if (attempted_ || cancelled_) return Unavailable();
  attempted_ = true;
  const bool bound = responses_native_.execute && responses_native_.acquire &&
                     responses_native_.release;
  auto reply = Connected(configuration, bound, decisions_native_.Bound(),
                         bool(recording_));
  if (!Accepted(reply)) return reply;
  auto callbacks = context_callbacks_;
  const auto mode = configuration.Find("delegation")->GetString("type");
  if (decisions_native_.Bound()) {
    decisions_ = std::make_unique<LiveDecisionDelegate>(
        std::move(decisions_native_), callbacks.emit,
        std::move(plan.decision_catalog), decision_items_,
        *configuration.Find("input"), message_bytes_);
    decisions_->Settings(configuration);
    if (mode == "decisions") {
      callbacks.decision_owner = [this](std::string_view id) {
        return decisions_->Known(id);
      };
      callbacks.delegate = [this](std::string id, Json task) {
        return decisions_->Delegate(std::move(id), std::move(task),
                                    frontend_.Timeline() / 48);
      };
    }
  }
  if (mode == "responses") {
    responses_ = std::make_unique<LiveResponsesDelegate>(
        std::move(responses_native_), callbacks.emit, [this](std::string text) {
          return context_->SpeakBackend(std::move(text));
        });
    responses_->Settings(*configuration.Find("delegation")->Find("responses"));
    callbacks.delegate = [this](std::string id, Json task) {
      return responses_->Delegate(std::move(id), std::move(task));
    };
  }
  context_ = std::make_unique<LiveClientContext>(id_, frontend_, plan.settings,
                                                 std::move(callbacks));
  if (!context_->Valid()) return Unavailable();
  reply = context_->AcknowledgementDelay(
      uint32_t(configuration.Find("delegation")
                   ->Find("acknowledgement_delay_ms")
                   ->number));
  if (!Accepted(reply)) return reply;
  reply = frontend_.Prepare(std::move(plan.settings), plan.input, plan.format);
  if (!Accepted(reply)) return reply;
  configuration_ = std::move(configuration);
  prepared_ = true;
  return {};
}
void NativeLiveClientBackend::ResponsesReady(bool ready) noexcept {
  if (responses_) responses_->Ready(ready);
}
ObjectReply NativeLiveClientBackend::Update(const Json& configuration) {
  if (!prepared_ || closing_ || cancelled_ || failed_) return Unavailable();
  auto next = configuration;
  auto reply =
      Connected(next, bool(responses_), bool(decisions_), bool(recording_));
  if (!Accepted(reply)) return reply;
  // The public Live configuration transaction already protects immutable
  // startup fields. Preserve appended private instructions across updates.
  for (const auto* name :
       {"model", "instructions", "input", "audio", "store"}) {
    const auto* old = configuration_.Find(name);
    const auto* selected = next.Find(name);
    if (!old || !selected || OutputJson(*old) != OutputJson(*selected))
      return Error(400, "immutable_field_update",
                   "Live startup fields are immutable");
  }
  reply = context_->AcknowledgementDelay(uint32_t(
      next.Find("delegation")->Find("acknowledgement_delay_ms")->number));
  if (Accepted(reply)) {
    if (responses_)
      responses_->Settings(*next.Find("delegation")->Find("responses"));
    if (decisions_) decisions_->Settings(next);
    configuration_ = std::move(next);
  }
  return reply;
}
ObjectReply NativeLiveClientBackend::Command(const Json& event) {
  if (!prepared_ || closing_ || cancelled_ || failed_) return Unavailable();
  const auto type = event.GetString("type");
  if (type == "decision.item.create" || type == "decision.item.delete") {
    auto reply = decision_items_.Apply(event, context_callbacks_.emit);
    if (Accepted(reply) && decisions_) decisions_->InputsChanged();
    return reply;
  }
  if (type == "decision.create" && decisions_)
    return decisions_->Create(event.GetString("event_id"),
                              frontend_.Timeline() / 48);
  if (type == "session.input_audio.append")
    return frontend_.AppendAudio(event.GetString("audio"),
                                 event.GetString("event_id"));
  if (type == "session.input_audio.mute" ||
      type == "session.input_audio.unmute")
    return frontend_.Mute(type == "session.input_audio.mute",
                          event.GetString("event_id"));
  if (type == "session.output_audio.fixture.play") {
    auto reply = frontend_.PlayFixture(event.GetString("audio"),
                                       event.GetString("event_id"));
    if (Accepted(reply) && responses_ && responses_->Running())
      responses_->Interrupt();
    return reply;
  }
  if (responses_ &&
      (type == "response.create" || type == "response.item.create")) {
    if (frontend_.FixtureActive())
      return Error(409, "live_fixture_active", "Audio fixture is active");
    return type == "response.create"
               ? responses_->Create(event.GetString("event_id"))
               : responses_->Add(*event.Find("item"));
  }
  if (type == "session.instructions.append" ||
      type == "session.thinking.append" || type == "session.commentary.append")
    return context_->Append(event);
  if (type == "response.create" || type == "response.item.create")
    return Error(400, "invalid_delegation_mode",
                 "Response commands require Responses delegation");
  return Missing(
      "type", "The requested native Live command processor is not connected");
}
ObjectReply NativeLiveClientBackend::Tick() {
  if (!prepared_ || cancelled_ || failed_) return Unavailable();
  auto reply =
      responses_ ? responses_->Tick(frontend_.Timeline() / 48) : ObjectReply{};
  if (Accepted(reply))
    reply = frontend_.Tick(!decisions_ || !decisions_->Executing());
  if (Accepted(reply) && decisions_)
    reply = decisions_->Tick(
        !frontend_.Generating() && !frontend_.InputExecuting() &&
        !frontend_.QueuedTurns() && (!responses_ || !responses_->Running()));
  if (Accepted(reply) && !closing_ && !frontend_.FixtureActive())
    reply = context_->Tick();
  if (Accepted(reply) && recording_) {
    if (MediaDrained()) recording_->Finish(frontend_.RecordingEndTicks());
    const auto error = recording_->TakeError();
    if (!error.ok()) {
      auto details = Object({{"type", String("server_error")},
                             {"param", String("session.store")},
                             {"code", String(std::string(error.ClientCode()))},
                             {"message", String(error.ClientMessage())}});
      reply = context_callbacks_.emit(
          Object({{"type", String("error")}, {"error", std::move(details)}}));
    }
  }
  if (!Accepted(reply)) {
    failed_ = true;
    Cancel();
  }
  return reply;
}
ObjectReply NativeLiveClientBackend::Close() {
  if (!prepared_ || cancelled_ || failed_) return Unavailable();
  if (closing_) return {};
  const auto reply = frontend_.Close();
  if (Accepted(reply)) {
    closing_ = true;
    if (responses_) responses_->Close();
    if (decisions_) decisions_->Close();
    context_->Close();
  }
  return reply;
}
bool NativeLiveClientBackend::Drained() const {
  return MediaDrained() && (!recording_ || recording_->Drained());
}
bool NativeLiveClientBackend::MediaDrained() const {
  return prepared_ && closing_ && !cancelled_ && !failed_ &&
         (!responses_ || !responses_->Running()) &&
         (!decisions_ || !decisions_->Running()) && frontend_.Drained();
}
void NativeLiveClientBackend::Cancel() noexcept {
  cancelled_ = true;
  if (recording_) recording_->Cancel();
  if (responses_) responses_->Interrupt();
  if (decisions_) decisions_->Cancel();
  if (context_) context_->Close();
  frontend_.Cancel();
}
void NativeLiveClientBackend::Join() noexcept {
  if (responses_) responses_->Join();
  if (decisions_) decisions_->Join();
  frontend_.Join();
  if (recording_) recording_->Join();
}
}  // namespace q4t::server
