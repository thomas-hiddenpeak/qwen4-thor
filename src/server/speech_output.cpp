#include "q4t/server/speech_output.h"

#include <cmath>

#include "object_protocol.h"
#include "q4t/audio/pcm_output.h"
#include "q4t/audio/speech_tempo.h"
#include "q4t/io/base64.h"
#include "q4t/trace/sha256.h"

namespace q4t::server {
namespace {
using object_detail::Error;
using Clock = std::chrono::steady_clock;
ObjectReply Invalid(const char* message) {
  return Error(502, "speech_output_invalid", message);
}
}  // namespace

ObjectReply WriteSpeechAudio(
    const SpeechRequest& request, size_t max_input_samples,
    const SpeechEncodeLimits& limits, const BinaryAccess& access,
    const SpeechPcmSource& source,
    const std::function<ObjectReply(std::string_view)>& sink,
    SpeechEncodingReceipt* receipt, GrammarControl control) {
  if (!receipt || !source.next || !sink || access.scope.empty() ||
      !ValidSpeechEncodeLimits(limits))
    return Invalid("Speech output owner or budget unavailable");
  audio::SpeechTempo tempo;
  if (!tempo.Init(request.speed, max_input_samples, limits.max_samples))
    return Invalid("Speech tempo capacity invalid");
  const auto check = [&]() -> ObjectReply {
    if ((access.cancelled && access.cancelled()) ||
        (control.cancelled && control.cancelled()))
      return Error(499, "request_cancelled", "Speech output cancelled");
    if (Clock::now() >= access.deadline)
      return Error(408, "request_deadline_exceeded", "Speech output timed out");
    return {};
  };
  if (auto state = check(); !state.ok()) return state;
  const bool native = request.format == SpeechFormat::kPcm ||
                      request.format == SpeechFormat::kWav;
  SpeechCodecStream codec;
  if (!native) {
    auto result = codec.Start(request.format, limits, access, control);
    if (!result.ok()) return result;
  }
  SpeechEncodingReceipt next;
  trace::Sha256Stream hash(limits.max_output_bytes);
  std::vector<float> pcm;
  std::optional<size_t> expected;
  bool source_final = false, wave_header = false;
  const auto write = [&](std::string_view bytes) -> ObjectReply {
    if (auto state = check(); !state.ok()) return state;
    if (bytes.size() > limits.max_output_bytes - hash.bytes())
      return Error(413, "speech_encoding_capacity",
                   "Speech output budget exceeded");
    auto result = sink(bytes);
    if (!result.ok()) return result;
    if (!hash.Add(
            {reinterpret_cast<const uint8_t*>(bytes.data()), bytes.size()}))
      return Invalid("Speech output digest capacity differs");
    return check();
  };
  const auto read_tempo = [&](std::span<const float>* output,
                              bool* done) -> ObjectReply {
    for (;;) {
      if (auto state = check(); !state.ok()) return state;
      audio::SpeechTempoStep step;
      if (!tempo.Advance(&step, output))
        return Invalid("Speech tempo execution failed");
      if (step == audio::SpeechTempoStep::kDone) {
        *done = true;
        return {};
      }
      if (step == audio::SpeechTempoStep::kOutput) return {};
      if (source_final) return Invalid("Speech source requested after EOF");
      pcm.clear();
      bool final = false;
      auto result = source.next(&pcm, &final);
      if (!result.ok()) return result;
      if (auto state = check(); !state.ok()) return state;
      if (pcm.size() > audio::SpeechTempo::kMaxAppend ||
          (pcm.empty() && !final) || !tempo.Append(pcm, final))
        return Invalid("Speech source PCM invalid or exceeds capacity");
      source_final = final;
      if (!wave_header && request.format == SpeechFormat::kWav) {
        expected = source.final_samples ? source.final_samples() : std::nullopt;
        if (!expected || !*expected || *expected > max_input_samples)
          return Invalid("WAVE requires the natural final sample count");
        const auto samples = static_cast<size_t>(
            std::ceil(static_cast<double>(*expected) / request.speed));
        std::string header;
        if (!audio::SpeechWaveHeader(samples, limits.max_output_bytes, &header))
          return Error(413, "speech_encoding_capacity",
                       "WAVE length exceeds budget");
        result = write(header);
        if (!result.ok()) return result;
        wave_header = true;
      }
      if (expected && tempo.input_samples() > *expected)
        return Invalid("Speech source exceeded the declared WAVE length");
    }
  };
  std::string pending;
  bool done = false;
  while (!done) {
    if (auto state = check(); !state.ok()) return state;
    if (!native) {
      SpeechCodecStep state;
      std::string_view bytes;
      auto result = codec.Advance(&state, &bytes);
      if (!result.ok()) return result;
      if (state == SpeechCodecStep::kOutput) {
        result = write(bytes);
        if (!result.ok()) return result;
        continue;
      }
      if (state == SpeechCodecStep::kDone) {
        next.codec = codec.Metadata();
        break;
      }
    }
    std::span<const float> output;
    auto result = read_tempo(&output, &done);
    if (!result.ok()) return result;
    if (native) {
      if (!output.empty()) {
        std::string bytes;
        if (!audio::EncodeSpeechPcm(output, false, output.size() * 2, &bytes))
          return Invalid("Speech tempo produced invalid PCM");
        pending += bytes;
      }
      if ((done || pending.size() >= 8192) && !pending.empty()) {
        result = write(pending);
        if (!result.ok()) return result;
        pending.clear();
      }
    } else {
      result = codec.Append(output, done);
      if (!result.ok()) return result;
      // Tempo EOF is only the codec input EOF; drain through verified Done.
      done = false;
    }
  }
  if (!source_final || !tempo.input_samples() || !tempo.output_samples() ||
      !hash.bytes() || (expected && tempo.input_samples() != *expected))
    return Invalid("Speech source did not complete the declared waveform");
  if (auto state = check(); !state.ok()) return state;
  next.input_samples = tempo.input_samples();
  next.output_samples = tempo.output_samples();
  next.bytes = hash.bytes();
  next.sha256 = trace::HexDigest(hash.Snapshot());
  *receipt = std::move(next);
  return {};
}

Status SpeechAudioDelta(std::string_view bytes, std::string* out) {
  if (!out || bytes.empty() || bytes.size() > SpeechCodecStream::kOutputBytes)
    return Status::Fail("Speech SSE audio chunk is invalid");
  std::string encoded;
  auto status = io::EncodeBase64(bytes, 4 * ((bytes.size() + 2) / 3), &encoded);
  if (!status) return status;
  *out =
      "event: speech.audio.delta\r\ndata: {\"type\":\"speech.audio.delta\","
      "\"audio\":\"" +
      encoded + "\"}\r\n\r\n";
  return {};
}
Status SpeechAudioDone(const ActualUsage& usage, std::string* out) {
  if (!out || !ValidActualUsage(usage))
    return Status::Fail("Speech SSE usage invalid");
  *out =
      "event: speech.audio.done\r\ndata: {\"type\":\"speech.audio.done\","
      "\"usage\":{\"input_tokens\":" +
      std::to_string(usage.input) +
      ",\"output_tokens\":" + std::to_string(usage.output) +
      ",\"total_tokens\":" + std::to_string(usage.input + usage.output) +
      "}}\r\n\r\n";
  return {};
}
}  // namespace q4t::server
