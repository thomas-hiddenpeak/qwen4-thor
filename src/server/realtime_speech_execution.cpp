#include "q4t/server/realtime_speech_execution.h"

#include <cmath>

#include "object_protocol.h"
#include "q4t/audio/tts_generation.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr size_t kSamples = 600ULL * 24000;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
const io::Json* Output(const io::Json& settings) {
  const auto* audio = settings.Find("audio");
  return audio ? audio->Find("output") : nullptr;
}
}  // namespace

Status PlanRealtimeSpeech(const io::Json& settings, const io::Json& response,
                          RealtimeSpeechOptions* out, ChatPlanError* error) {
  if (!out || !error) return Status::Fail("Missing speech plan owner");
  const auto invalid = [&](std::string path, std::string message) {
    *error = {"invalid_request", std::move(path), 400};
    return Status::Fail(std::move(message));
  };
  RealtimeSpeechOptions next;
  const auto* modes = response.Find("output_modalities");
  if (!modes) modes = settings.Find("output_modalities");
  if (modes) {
    if (!modes->IsArray() || modes->array.size() != 1 ||
        !modes->array[0].IsString() ||
        (modes->array[0].str != "text" && modes->array[0].str != "audio"))
      return invalid("output_modalities", "Expected text or audio output");
    next.enabled = modes->array[0].str == "audio";
  }
  auto selected = Object({{"voice", String("vivian")},
                          {"format", Object({{"type", String("audio/pcm")},
                                             {"rate", Number(24000)}})},
                          {"speed", Number(1)}});
  for (const auto* source : {Output(settings), Output(response)}) {
    if (!source) continue;
    if (!source->IsObject())
      return invalid("audio.output", "Expected output options");
    for (const auto& [name, value] : source->object) {
      if (source == Output(response) && name == "speed")
        return invalid("audio.output.speed", "Speed is a session setting");
      if (name != "voice" && name != "format" && name != "speed")
        return invalid("audio.output." + name, "Unknown output option");
      Set(&selected, name, value);
    }
  }
  const auto* format = selected.Find("format");
  if (!format || !format->IsObject())
    return invalid("audio.output.format", "Expected an audio format");
  if (format->Has("type") && !format->Find("type")->IsString())
    return invalid("audio.output.format.type", "Expected a format name");
  const auto type = format->GetString("type", "audio/pcm");
  if (type == "audio/pcm") {
    if (const auto* rate = format->Find("rate");
        rate && (!rate->IsNumber() || rate->number != 24000))
      return invalid("audio.output.format.rate", "PCM requires 24000 Hz");
  } else if (type == "audio/pcmu" || type == "audio/pcma") {
    next.format = {type == "audio/pcmu" ? RealtimeAudioEncoding::kMuLaw
                                        : RealtimeAudioEncoding::kALaw,
                   8000};
    if (format->Has("rate"))
      return invalid("audio.output.format.rate", "G.711 has a fixed rate");
  } else {
    return invalid("audio.output.format.type", "Unknown output encoding");
  }
  for (const auto& [key, value] : format->object) {
    (void)value;
    if (key != "type" && key != "rate")
      return invalid("audio.output.format." + key, "Unknown format option");
  }
  const auto* speed = selected.Find("speed");
  if (!speed || !speed->IsNumber() || !std::isfinite(speed->number) ||
      speed->number < 0.25 || speed->number > 1.5)
    return invalid("audio.output.speed", "Expected a speed from 0.25 to 1.5");
  auto wire = Object({{"modalities", Array({String("text"), String("audio")})},
                      {"audio", Object({{"format", String("pcm16")},
                                        {"voice", *selected.Find("voice")}})}});
  auto status = ParseChatAudioOptions(wire, &next.chat, error);
  if (!status) {
    error->param = error->param.starts_with("audio.")
                       ? "audio.output." + error->param.substr(6)
                       : "audio.output";
    return status;
  }
  if (!next.chat.speech.voice_id &&
      audio::TtsBuiltinVoiceId(next.chat.speech.voice) < 0)
    return invalid("audio.output.voice",
                   "Requested native voice is unavailable");
  next.chat.enabled = next.enabled;
  next.chat.speech.speed = speed->number;
  *out = std::move(next);
  *error = {};
  return {};
}

bool SameRealtimeVoice(const RealtimeSpeechOptions& left,
                       const RealtimeSpeechOptions& right) {
  if (left.chat.speech.voice_id != right.chat.speech.voice_id) return false;
  if (left.chat.speech.voice_id)
    return left.chat.speech.voice == right.chat.speech.voice;
  const auto id = audio::TtsBuiltinVoiceId(left.chat.speech.voice);
  return id >= 0 && id == audio::TtsBuiltinVoiceId(right.chat.speech.voice);
}

RealtimeSpeechExecution::RealtimeSpeechExecution(
    const RealtimeSpeechOptions& options, RealtimeSpeechSink sink,
    std::function<bool()> cancelled)
    : encoder_(options.format, kSamples, kRealtimeAudioBytes, cancelled),
      hash_(kSamples * 2),
      sink_(std::move(sink)),
      cancelled_(std::move(cancelled)) {
  valid_ = options.enabled && options.chat.enabled && options.chat.text &&
           options.chat.speech.format == SpeechFormat::kPcm &&
           encoder_.Valid() && sink_;
}
ObjectReply RealtimeSpeechExecution::Fail(std::string_view message) {
  if (error_.ok())
    error_ =
        Error(502, "realtime_speech_receipt_invalid", std::string(message));
  return error_;
}
ObjectReply RealtimeSpeechExecution::Emit(const RealtimeSpeechEvent& event) {
  if (!Accepted(error_)) return error_;
  try {
    if (cancelled_ && cancelled_()) return Fail("Speech delivery cancelled");
    auto reply = sink_(event);
    if (!Accepted(reply)) {
      if (reply.code.empty()) {
        reply.code = "realtime_speech_sink_unconfirmed";
        reply.message = "Speech delivery was not confirmed";
        reply.http_status = 503;
      }
      error_ = std::move(reply);
    }
  } catch (...) {
    return Fail("Speech delivery callback failed");
  }
  return error_;
}
ObjectReply RealtimeSpeechExecution::Append(std::string_view pcm) {
  if (!Accepted(error_)) return error_;
  if (!valid_ || completed_) return Fail("Speech owner is not active");
  if (!hash_.Add({reinterpret_cast<const uint8_t*>(pcm.data()), pcm.size()}))
    return Fail("Speech PCM exceeds its byte capacity");
  auto reply = encoder_.Append(pcm, [&](std::string_view bytes) {
    return Emit(
        {RealtimeSpeechEvent::Kind::kChunk, std::string(bytes), {}, {}});
  });
  if (!Accepted(reply)) error_ = std::move(reply);
  return error_;
}
ObjectReply RealtimeSpeechExecution::Finish(std::string_view transcript,
                                            const SpeechEncodingReceipt& native,
                                            const ActualUsage& usage) {
  if (!Accepted(error_)) return error_;
  if (!valid_ || completed_ || transcript.empty() ||
      transcript.size() > 1ULL << 20 || !native.input_samples ||
      native.input_samples > kSamples || !native.output_samples ||
      native.output_samples > kSamples ||
      native.bytes != native.output_samples * 2 ||
      native.bytes != hash_.bytes() ||
      native.sha256 != trace::HexDigest(hash_.Snapshot()) || !usage.input ||
      !usage.output)
    return Fail("Native speech EOF does not match actual PCM and usage");
  RealtimeSpeechReceipt receipt;
  auto reply = encoder_.Finish(
      [&](std::string_view bytes) {
        return Emit(
            {RealtimeSpeechEvent::Kind::kChunk, std::string(bytes), {}, {}});
      },
      &receipt);
  if (!Accepted(reply)) {
    error_ = std::move(reply);
    return error_;
  }
  if (receipt.input_samples != native.output_samples)
    return Fail("Converted waveform differs from native PCM extent");
  reply = Emit(
      {RealtimeSpeechEvent::Kind::kDone, {}, std::string(transcript), receipt});
  if (Accepted(reply)) {
    completed_ = true;
    usage_ = usage;
  }
  return reply;
}
}  // namespace q4t::server
