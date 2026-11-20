#include "q4t/server/live_native_backend.h"

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
ObjectReply Connected(const Json& configuration) {
  const auto* delegation = configuration.Find("delegation");
  const auto* triggers = configuration.GetArray("triggers");
  if (!delegation || delegation->GetString("type") != "client")
    return Missing("session.delegation",
                   "Requested native delegation processor is not connected");
  if (!triggers || !triggers->array.empty())
    return Missing("session.triggers",
                   "Native Decisions trigger processor is not connected");
  if (configuration.GetBool("store"))
    return Missing("session.store",
                   "Native Live recording owner is not connected");
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
                            NativeLivePlan* output, ChatPlanError* error) {
  if (!output || !error) return Status::Fail("Missing native Live plan owner");
  *error = {};
  auto status = ValidateLiveShape(configuration, LiveShape::kSessionStart,
                                  error, "session");
  if (!status) return status;
  const auto connected = Connected(configuration);
  if (!Accepted(connected)) {
    *error = {connected.code, connected.value.GetString("param"),
              connected.http_status};
    return Status::Fail(connected.message);
  }
  NativeLivePlan next;
  next.settings =
      DefaultRealtimeChatSettings(planning, configuration.GetString("model"));
  const auto* instructions = configuration.Find("instructions");
  const auto* input = configuration.GetArray("input");
  const auto* audio = configuration.Find("audio");
  const auto* format = audio ? audio->Find("format") : nullptr;
  const auto* speech = audio ? audio->Find("output") : nullptr;
  const auto* voice = speech ? speech->Find("voice") : nullptr;
  if (!instructions || !instructions->IsString() || !input || !format ||
      !voice) {
    *error = {"invalid_request", "session", 400};
    return Status::Fail("Native Live requires resolved startup configuration");
  }
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
    size_t message_bytes)
    : id_(std::move(id)),
      context_callbacks_(std::move(context)),
      frontend_(id_, Bind(std::move(native)), max_audio_seconds,
                message_bytes) {}
NativeLiveClientBackend::~NativeLiveClientBackend() {
  Cancel();
  Join();
}
LiveFrontendNative NativeLiveClientBackend::Bind(LiveFrontendNative native) {
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
  auto reply = Connected(configuration);
  if (!Accepted(reply)) return reply;
  context_ = std::make_unique<LiveClientContext>(id_, frontend_, plan.settings,
                                                 context_callbacks_);
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
ObjectReply NativeLiveClientBackend::Update(const Json& configuration) {
  if (!prepared_ || closing_ || cancelled_ || failed_) return Unavailable();
  auto next = configuration;
  auto reply = Connected(next);
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
  if (Accepted(reply)) configuration_ = std::move(next);
  return reply;
}
ObjectReply NativeLiveClientBackend::Command(const Json& event) {
  if (!prepared_ || closing_ || cancelled_ || failed_) return Unavailable();
  const auto type = event.GetString("type");
  if (type == "session.input_audio.append")
    return frontend_.AppendAudio(event.GetString("audio"),
                                 event.GetString("event_id"));
  if (type == "session.input_audio.mute" ||
      type == "session.input_audio.unmute")
    return frontend_.Mute(type == "session.input_audio.mute",
                          event.GetString("event_id"));
  if (type == "session.instructions.append" ||
      type == "session.thinking.append" || type == "session.commentary.append")
    return context_->Append(event);
  if (type == "response.create" || type == "response.item.create")
    return Error(400, "invalid_delegation_mode",
                 "Response commands require Responses delegation");
  // Fixture/Decisions execution owners are the next implementation groups.
  // Do not issue playing/played or response success for unexecuted commands.
  return Missing(
      "type", "The requested native Live command processor is not connected");
}
ObjectReply NativeLiveClientBackend::Tick() {
  if (!prepared_ || cancelled_ || failed_) return Unavailable();
  auto reply = frontend_.Tick();
  if (Accepted(reply) && !closing_) reply = context_->Tick();
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
    context_->Close();
  }
  return reply;
}
bool NativeLiveClientBackend::Drained() const {
  return prepared_ && closing_ && !cancelled_ && !failed_ &&
         frontend_.Drained();
}
void NativeLiveClientBackend::Cancel() noexcept {
  cancelled_ = true;
  if (context_) context_->Close();
  frontend_.Cancel();
}
void NativeLiveClientBackend::Join() noexcept { frontend_.Join(); }
}  // namespace q4t::server
