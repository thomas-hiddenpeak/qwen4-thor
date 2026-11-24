#include "q4t/server/realtime_translation_session.h"

#include <algorithm>
#include <cmath>

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/realtime_client_secrets.h"
#include "q4t/server/realtime_transcription_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr size_t kRate = 16000;
ObjectReply Unavailable() {
  return Error(503, "translation_unavailable",
               "Translation session unavailable");
}
}  // namespace

RealtimeTranslationSession::RealtimeTranslationSession(
    std::string id, TranslationEventSink output,
    RealtimeTranslationNative native, size_t message_bytes,
    uint32_t max_audio_seconds)
    : id_(std::move(id)),
      output_(std::move(output)),
      native_(std::move(native)),
      message_bytes_(message_bytes),
      max_samples_((std::min<uint32_t>(6, max_audio_seconds) * kRate / 512) *
                   512) {}

bool RealtimeTranslationSession::Start() {
  return StartConfigured(
             Object(
                 {{"model", String(std::string(kRealtimeTranslationModel))}}))
      .ok();
}
ObjectReply RealtimeTranslationSession::StartConfigured(const Json& startup) {
  if (processor_ || !output_ || !native_.acquire || !native_.release ||
      !native_.execute || !message_bytes_ || !max_samples_)
    return Unavailable();
  auto reply = settings_.Apply(startup, true);
  if (!reply.ok()) return reply;
  expires_ = RealtimeSecretTime::Now().unix_seconds + 3600;
  segment_.reserve(max_samples_);
  ResetProcessor(0);
  if (!processor_->Valid() ||
      !Emit(Object({{"type", String("session.created")},
                    {"session", settings_.Resource(id_, expires_)}})))
    return Unavailable();
  return {};
}
void RealtimeTranslationSession::ResetProcessor(uint64_t origin) {
  processor_ = std::make_unique<RealtimeNoiseInput>(
      RealtimeAudioFormat{}, settings_.noise, origin,
      [this] { return interrupted_.load(); }, settings_.strength);
  vad_ = {};
}
bool RealtimeTranslationSession::Emit(Json event) {
  Set(&event, "event_id", String(detail::NewRequestId("event_")));
  return !interrupted_.load() && output_(std::move(event));
}
bool RealtimeTranslationSession::Report(const ObjectReply& reply,
                                        const std::string& client_event) {
  auto error = Object(
      {{"type", String(reply.http_status >= 500 ? "server_error"
                                                : "invalid_request_error")},
       {"code", String(std::string(reply.ClientCode()))},
       {"message", String(reply.ClientMessage())}});
  if (!client_event.empty()) Set(&error, "event_id", String(client_event));
  if (const auto* param = reply.value.Find("param"))
    Set(&error, "param", *param);
  return Emit(Object({{"type", String("error")}, {"error", std::move(error)}}));
}
bool RealtimeTranslationSession::Fail(const ObjectReply& reply,
                                      const std::string& client_event) {
  if (!Report(reply, client_event)) return false;
  failed_.store(true);
  return true;
}
void RealtimeTranslationSession::Interrupt() {
  interrupted_.store(true);
  std::lock_guard lock(mutex_);
  if (active_) active_->Cancel(RequestControl::State::kDisconnect);
}

ObjectReply RealtimeTranslationSession::Translate() {
  ScopeExit reset([&] {
    segment_.clear();
    voiced_ = false;
    quiet_samples_ = 0;
  });
  if (!voiced_ || segment_.empty()) return {};
  auto control = native_.acquire(detail::NewRequestId("req-"));
  if (!control) return Unavailable();
  ScopeExit release([&] {
    {
      std::lock_guard lock(mutex_);
      active_.reset();
    }
    native_.release(control);
  });
  {
    std::lock_guard lock(mutex_);
    if (interrupted_.load()) return Unavailable();
    active_ = control;
  }
  return native_.execute(segment_, settings_, control,
                         [this](Json event) { return Emit(std::move(event)); });
}
Status RealtimeTranslationSession::Frame(std::span<const float> frame) {
  // Keep a short prefix before speech. Finish a clause on silence and cap
  // uninterrupted speech so a continuous speaker gets output before close.
  double energy = 0;
  for (float sample : frame) energy += double(sample) * sample;
  const bool audible = energy > frame.size() * 1e-6;
  bool speech = audible;
  if (native_.vad && frame.size() == frame_.size()) {
    float probability = 0;
    auto status = native_.vad(frame, &vad_, &probability);
    if (!status) return status;
    speech = probability >= .5f;
  }
  voiced_ |= speech;
  quiet_samples_ = speech ? 0 : quiet_samples_ + frame.size();
  segment_.insert(segment_.end(), frame.begin(), frame.end());
  if (!voiced_ && segment_.size() > kRate / 2) {
    segment_.erase(segment_.begin(), segment_.begin() + frame.size());
  } else if (segment_.size() >= max_samples_ ||
             (voiced_ && quiet_samples_ >= kRate / 3)) {
    failure_ = Translate();
    if (!failure_.ok()) return Status::Fail(failure_.ClientMessage());
  }
  return {};
}
Status RealtimeTranslationSession::Collect(std::span<const float> samples) {
  while (!samples.empty()) {
    const auto count = std::min(samples.size(), frame_.size() - filled_);
    std::copy_n(samples.begin(), count, frame_.begin() + filled_);
    filled_ += count;
    samples = samples.subspan(count);
    if (filled_ == frame_.size()) {
      filled_ = 0;
      auto status = Frame(frame_);
      if (!status) return status;
    }
  }
  return {};
}
ObjectReply RealtimeTranslationSession::FinishInput() {
  if (processor_->PartialSample())
    return Error(400, "invalid_audio", "PCM input ended within a sample");
  auto status =
      processor_->Finish([this](auto samples) { return Collect(samples); });
  if (status && filled_) {
    status = Frame(std::span(frame_).first(filled_));
    filled_ = 0;
  }
  if (!status) return failure_.ok() ? Unavailable() : failure_;
  return Translate();
}
bool RealtimeTranslationSession::Message(WebSocketOpcode opcode,
                                         std::string_view message) {
  if (completed_.load() || failed_.load()) return true;
  if (!processor_ || interrupted_.load()) return false;
  Json event;
  ChatPlanError error;
  if (opcode != WebSocketOpcode::kText || message.size() > message_bytes_ ||
      !io::ParseJson(std::string(message), &event, true,
                     {32, 1024, message_bytes_, true}))
    return Report(
        Error(400, "invalid_request", "Invalid translation event JSON"), {});
  const auto id = event.GetString("event_id");
  const auto shape = ValidateRealtimeWireShape(
      event, RealtimeShape::kTranslationClientEvent, &error, "event");
  if (!shape) return Report(Error(400, "invalid_request", shape.message()), id);
  const auto type = event.GetString("type");
  if (type == "session.input_audio_buffer.append") {
    std::string pcm;
    auto reply = DecodeRealtimeAudioBase64(event.GetString("audio"), &pcm);
    if (!reply.ok() || pcm.empty())
      return Report(Error(400, "invalid_audio", "Expected PCM16 audio"), id);
    auto status = processor_->Append(
        {reinterpret_cast<const uint8_t*>(pcm.data()), pcm.size()},
        [this](auto samples) { return Collect(samples); });
    if (status) return true;
    return Fail(failure_.ok() ? Unavailable() : failure_, id);
  }
  if (type == "session.update") {
    auto next = settings_;
    auto reply = next.Apply(*event.Find("session"), false);
    if (!reply.ok()) return Report(reply, id);
    // Flush audio accepted under the old configuration before acknowledging
    // a changed language or filter. Never relabel already accepted speech.
    if (next.language != settings_.language || next.noise != settings_.noise ||
        next.strength != settings_.strength ||
        next.transcription != settings_.transcription) {
      const auto origin = processor_->AcceptedTicks();
      reply = FinishInput();
      if (!reply.ok())
        return processor_->PartialSample() ? Report(reply, id)
                                           : Fail(reply, id);
      settings_ = std::move(next);
      ResetProcessor(origin);
    }
    return Emit(Object({{"type", String("session.updated")},
                        {"session", settings_.Resource(id_, expires_)}}));
  }
  auto reply = FinishInput();
  if (!reply.ok())
    return processor_->PartialSample() ? Report(reply, id) : Fail(reply, id);
  if (!Emit(Object({{"type", String("session.closed")}}))) return false;
  completed_.store(true);
  return true;
}
}  // namespace q4t::server
