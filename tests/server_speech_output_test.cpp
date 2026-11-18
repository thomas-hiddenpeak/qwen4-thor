#include "q4t/server/speech_output.h"

#include <array>
#include <cmath>
#include <limits>
#include <numbers>

#include "../src/server/object_protocol.h"
#include "q4t/audio/pcm_output.h"
#include "q4t/io/base64.h"
#include "q4t/server/file_audio.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
std::vector<float> Samples(size_t count = 24000) {
  std::vector<float> result(count);
  for (size_t i = 0; i < count; ++i)
    result[i] = .5 * std::sin(2 * std::numbers::pi * 440 * i / 24000);
  return result;
}
SpeechPcmSource Source(std::span<const float> samples,
                       std::optional<size_t> declared, size_t* offset) {
  return {[samples, offset](std::vector<float>* out, bool* final) {
            const size_t count =
                std::min<size_t>(997, samples.size() - *offset);
            out->assign(samples.begin() + *offset,
                        samples.begin() + *offset + count);
            *offset += count;
            *final = *offset == samples.size();
            return ObjectReply{};
          },
          [declared] { return declared; }};
}
BinaryAccess Access() { return {"speech:client", Clock::now() + 5s, {}}; }
}  // namespace

Q4T_TEST(base64_shared_canonical_vectors_and_budget_are_atomic) {
  constexpr std::array<std::pair<std::string_view, std::string_view>, 7> cases{
      {{"", ""},
       {"f", "Zg=="},
       {"fo", "Zm8="},
       {"foo", "Zm9v"},
       {"foob", "Zm9vYg=="},
       {"fooba", "Zm9vYmE="},
       {"foobar", "Zm9vYmFy"}}};
  for (const auto& [raw, encoded] : cases) {
    std::string out;
    Q4T_CHECK(q4t::io::EncodeBase64(raw, encoded.size(), &out));
    Q4T_CHECK(out == encoded);
    if (!encoded.empty()) {
      Q4T_CHECK(!q4t::io::EncodeBase64(raw, encoded.size() - 1, &out));
      Q4T_CHECK(out == encoded);
    }
  }
  std::string out;
  Q4T_CHECK(q4t::io::EncodeBase64(std::string("\x00\xff\x80", 3), 4, &out));
  Q4T_CHECK(out == "AP+A");
  return true;
}
Q4T_TEST(
    speech_output_native_stream_matches_complete_pcm_and_real_wave_header) {
  const auto source = Samples();
  for (auto format : {SpeechFormat::kPcm, SpeechFormat::kWav}) {
    size_t offset = 0, calls = 0;
    std::string bytes;
    SpeechRequest request;
    request.format = format;
    SpeechEncodingReceipt receipt;
    const auto sink = [&](std::string_view block) {
      Q4T_CHECK(block.size() <= 8672);
      return true;
    };
    auto result = WriteSpeechAudio(
        request, source.size(), {}, Access(),
        Source(source, source.size(), &offset),
        [&](std::string_view block) {
          if (!sink(block)) return Error(500, "oversize", "large stream block");
          bytes += block;
          ++calls;
          return ObjectReply{};
        },
        &receipt);
    Q4T_CHECK(result.ok() && calls > 2 &&
              receipt.input_samples == source.size());
    std::string expected;
    Q4T_CHECK(q4t::audio::EncodeSpeechPcm(source, format == SpeechFormat::kWav,
                                          48044, &expected));
    Q4T_CHECK(bytes == expected && receipt.bytes == bytes.size() &&
              receipt.sha256 == Digest(bytes));
  }
  return true;
}
Q4T_TEST(speech_output_wave_requires_natural_length_and_checks_actual_count) {
  const auto source = Samples(2507);
  for (auto declared : {std::optional<size_t>{}, std::optional<size_t>{2000},
                        std::optional<size_t>{3000}}) {
    size_t offset = 0;
    SpeechRequest request;
    request.format = SpeechFormat::kWav;
    SpeechEncodingReceipt receipt;
    receipt.sha256 = "preserved";
    std::string bytes;
    auto result = WriteSpeechAudio(
        request, 4000, {}, Access(), Source(source, declared, &offset),
        [&](std::string_view block) {
          bytes += block;
          return ObjectReply{};
        },
        &receipt);
    Q4T_CHECK(!result.ok() && receipt.sha256 == "preserved");
    if (!declared) Q4T_CHECK(bytes.empty());
  }
  for (double speed : {.25, 1., 2., 4.}) {
    size_t offset = 0;
    SpeechRequest request;
    request.format = SpeechFormat::kWav;
    request.speed = speed;
    SpeechEncodingReceipt receipt;
    std::string bytes;
    Q4T_CHECK(WriteSpeechAudio(
                  request, 4000, {}, Access(),
                  Source(source, source.size(), &offset),
                  [&](std::string_view block) {
                    bytes += block;
                    return ObjectReply{};
                  },
                  &receipt)
                  .ok());
    const auto expected = size_t(std::ceil(source.size() / speed));
    std::string header;
    Q4T_CHECK(q4t::audio::SpeechWaveHeader(expected, 50000, &header));
    Q4T_CHECK(bytes.size() == 44 + expected * 2 && bytes.starts_with(header) &&
              receipt.output_samples == expected);
  }
  return true;  // Length contract only; no 4x spoken-content qualification.
}
Q4T_TEST(speech_output_compressed_composition_matches_complete_encoder) {
  const auto source = Samples();
  for (auto format : {SpeechFormat::kMp3, SpeechFormat::kAac,
                      SpeechFormat::kFlac, SpeechFormat::kOpus}) {
    size_t offset = 0;
    SpeechRequest request;
    request.format = format;
    SpeechEncodingReceipt receipt;
    std::string bytes;
    GrammarControl control;
    control.counters = std::make_shared<GrammarWorkerCounters>();
    Q4T_CHECK(WriteSpeechAudio(
                  request, source.size(), {}, Access(),
                  Source(source, std::nullopt, &offset),
                  [&](std::string_view block) {
                    bytes += block;
                    return ObjectReply{};
                  },
                  &receipt, control)
                  .ok());
    Q4T_CHECK(control.counters->active == 0 && control.counters->reaped == 1);
    EncodedSpeech full;
    Q4T_CHECK(EncodeSpeech(source, format, {}, Access(), &full).ok());
    Q4T_CHECK(receipt.sha256 == Digest(bytes) &&
              receipt.output_samples == source.size());
    if (format == SpeechFormat::kMp3 || format == SpeechFormat::kAac) {
      Q4T_CHECK(bytes == full.bytes);
    } else {
      // Container IDs/length metadata need not be identical to a finite
      // encoder. Actual decoded samples must match, without trimming.
      FileAudioPcm a, b;
      Q4T_CHECK(DecodeAudioContainer(bytes, {}, Access(), &a).ok());
      Q4T_CHECK(DecodeAudioContainer(full.bytes, {}, Access(), &b).ok());
      Q4T_CHECK(a.samples == b.samples);
    }
  }
  return true;
}
Q4T_TEST(speech_output_failure_cancel_budget_and_source_fault_never_finish) {
  const auto source = Samples();
  for (int fault = 0; fault < 5; ++fault) {
    size_t offset = 0, calls = 0;
    bool cancelled = false;
    auto access = Access();
    access.cancelled = [&] { return cancelled; };
    SpeechRequest request;
    request.format = SpeechFormat::kPcm;
    SpeechEncodeLimits limits;
    if (fault == 0) limits.max_output_bytes = 20000;
    auto input = Source(source, source.size(), &offset);
    if (fault == 3)
      input.next = [](std::vector<float>*, bool*) {
        return Error(503, "gpu_unhealthy", "actual producer failure");
      };
    if (fault == 4)
      input.next = [](std::vector<float>* out, bool* final) {
        *out = {std::numeric_limits<float>::quiet_NaN()};
        *final = false;
        return ObjectReply{};
      };
    SpeechEncodingReceipt receipt;
    receipt.sha256 = "preserved";
    const auto result = WriteSpeechAudio(
        request, source.size(), limits, access, input,
        [&](std::string_view) {
          ++calls;
          if (fault == 1)
            return Error(409, "write_failed", "sink rejected output");
          if (fault == 2) cancelled = true;
          return ObjectReply{};
        },
        &receipt);
    Q4T_CHECK(!result.ok() && receipt.sha256 == "preserved");
    if (fault == 0) Q4T_CHECK(result.http_status == 413 && calls > 0);
    if (fault == 1) Q4T_CHECK(result.code == "write_failed");
    if (fault == 2) Q4T_CHECK(result.http_status == 499);
    if (fault == 3) Q4T_CHECK(result.code == "gpu_unhealthy" && calls == 0);
    if (fault == 4) Q4T_CHECK(result.http_status == 502 && calls == 0);
  }
  return true;
}
Q4T_TEST(speech_sse_contract_has_real_bytes_and_exact_terminal_usage) {
  std::string event;
  Q4T_CHECK(SpeechAudioDelta(std::string("\x00\xff\x80", 3), &event));
  Q4T_CHECK(event ==
            "event: speech.audio.delta\r\ndata: "
            "{\"type\":\"speech.audio.delta\",\"audio\":\"AP+A\"}\r\n\r\n");
  const auto previous = event;
  Q4T_CHECK(!SpeechAudioDelta({}, &event) && event == previous);
  Q4T_CHECK(SpeechAudioDone({17, 641}, &event));
  Q4T_CHECK(event ==
            "event: speech.audio.done\r\ndata: {\"type\":\"speech.audio.done\","
            "\"usage\":{\"input_tokens\":17,\"output_tokens\":641,"
            "\"total_tokens\":658}}\r\n\r\n");
  Q4T_CHECK(event.find("[DONE]") == event.npos);
  const auto done = event;
  Q4T_CHECK(!SpeechAudioDone({kActualUsageLimit, 1}, &event) && event == done);
  return true;
}
