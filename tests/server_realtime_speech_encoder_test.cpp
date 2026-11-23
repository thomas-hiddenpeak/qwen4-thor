#include "q4t/server/realtime_speech_encoder.h"

#include <algorithm>
#include <cmath>
#include <numbers>
#include <stdexcept>
#include <type_traits>

#include "q4t/server/realtime_pcm.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
static_assert(!std::is_copy_constructible_v<RealtimeSpeechEncoder>);
static_assert(!std::is_copy_assignable_v<RealtimeSpeechEncoder>);
std::string Pcm(size_t count) {
  std::string bytes;
  for (size_t i = 0; i < count; ++i) {
    const auto value = uint16_t(
        int16_t(16000 * std::sin(2 * std::numbers::pi * 440 * i / 24000) +
                7000 * std::cos(2 * std::numbers::pi * 10000 * i / 24000)));
    bytes.push_back(char(value & 255));
    bytes.push_back(char(value >> 8));
  }
  return bytes;
}
bool Encode(std::string_view input, RealtimeAudioFormat format, size_t chunk,
            std::string* output) {
  RealtimeSpeechEncoder encoder(format, input.size() / 2, input.size());
  Q4T_CHECK(encoder.Valid());
  const auto sink = [&](std::string_view bytes) {
    ObjectReply reply;
    if (bytes.empty() || bytes.size() > encoder.kOutputChunkBytes)
      reply.code = "fixture_chunk_invalid";
    output->append(bytes);
    return reply;
  };
  for (size_t offset = 0; offset < input.size(); offset += chunk)
    Q4T_CHECK(encoder.Append(input.substr(offset, chunk), sink).ok());
  RealtimeSpeechReceipt receipt;
  Q4T_CHECK(encoder.Finish(sink, &receipt).ok());
  Q4T_CHECK(encoder.Finished());
  Q4T_CHECK(receipt.input_samples == input.size() / 2);
  Q4T_CHECK(receipt.output_samples == input.size() / 2 * format.rate / 24000);
  Q4T_CHECK(receipt.bytes == output->size());
  return true;
}
}  // namespace

Q4T_TEST(realtime_speech_pcm_preserves_every_s16_value_across_odd_chunks) {
  std::string input;
  for (int sample = -32768; sample <= 32767; ++sample) {
    const auto value = uint16_t(sample);
    input.push_back(char(value & 255));
    input.push_back(char(value >> 8));
  }
  for (const size_t chunk : {1, 3, 2047, 65536}) {
    std::string output;
    Q4T_CHECK(Encode(input, {}, chunk, &output));
    Q4T_CHECK(output == input);
  }
  return true;
}

Q4T_TEST(realtime_speech_g711_preserves_phase_and_exact_tail) {
  const auto input = Pcm(24017);
  for (const auto kind :
       {RealtimeAudioEncoding::kMuLaw, RealtimeAudioEncoding::kALaw}) {
    std::string reference;
    Q4T_CHECK(Encode(input, {kind, 8000}, input.size(), &reference));
    Q4T_CHECK(reference.size() == 8005);
    for (const size_t chunk : {1, 2, 17, 2047}) {
      std::string output;
      Q4T_CHECK(Encode(input, {kind, 8000}, chunk, &output));
      Q4T_CHECK(output == reference);
    }
  }
  return true;
}

Q4T_TEST(realtime_speech_g711_silence_and_dc_have_known_wire_codes) {
  for (const auto kind :
       {RealtimeAudioEncoding::kMuLaw, RealtimeAudioEncoding::kALaw}) {
    std::string silence;
    Q4T_CHECK(Encode(std::string(10000, '\0'), {kind, 8000}, 7, &silence));
    Q4T_CHECK(
        silence ==
        std::string(1666,
                    char(kind == RealtimeAudioEncoding::kMuLaw ? 255 : 213)));
    std::string input;
    for (size_t i = 0; i < 6000; ++i) input += std::string("\0\x20", 2);
    std::string output;
    Q4T_CHECK(Encode(input, {kind, 8000}, 11, &output));
    const auto expected = kind == RealtimeAudioEncoding::kMuLaw ? 159 : 181;
    for (size_t i = 100; i + 100 < output.size(); ++i)
      Q4T_CHECK(uint8_t(output[i]) == expected);
  }
  return true;
}

Q4T_TEST(realtime_speech_exact_budgets_reject_before_additional_output) {
  const auto input = Pcm(4097);
  for (const size_t budget : {size_t(8193), size_t(8194)}) {
    RealtimeSpeechEncoder encoder({}, 4097, budget);
    std::string output;
    const auto sink = [&](std::string_view bytes) {
      output += bytes;
      return ObjectReply{};
    };
    Q4T_CHECK(
        encoder.Append(std::string_view(input).substr(0, 8192), sink).ok());
    Q4T_CHECK(output.size() == 8192);
    const auto reply =
        encoder.Append(std::string_view(input).substr(8192), sink);
    RealtimeSpeechReceipt receipt{91, 92, 93};
    if (budget == 8193) {
      Q4T_CHECK(!reply.ok() && output.size() == 8192);
      Q4T_CHECK(!encoder.Finish(sink, &receipt).ok());
      Q4T_CHECK(receipt.bytes == 93 && !encoder.Finished());
    } else {
      Q4T_CHECK(reply.ok() && encoder.Finish(sink, &receipt).ok());
      Q4T_CHECK(output == input && receipt.bytes == 8194);
    }
  }
  RealtimeSpeechEncoder limited({}, 1, 2);
  size_t calls = 0;
  const auto sink = [&](std::string_view) {
    ++calls;
    return ObjectReply{};
  };
  Q4T_CHECK(!limited.Append(std::string(3, '\0'), sink).ok());
  Q4T_CHECK(calls == 0);
  return true;
}

Q4T_TEST(realtime_speech_incomplete_eof_does_not_publish_receipt) {
  for (const auto& input : {std::string(), std::string(3, '\0')}) {
    RealtimeSpeechEncoder encoder({}, 8, 16);
    const auto sink = [](std::string_view) { return ObjectReply{}; };
    if (!input.empty()) Q4T_CHECK(encoder.Append(input, sink).ok());
    RealtimeSpeechReceipt receipt{91, 92, 93};
    Q4T_CHECK(!encoder.Finish(sink, &receipt).ok());
    Q4T_CHECK(receipt.input_samples == 91 && receipt.output_samples == 92 &&
              receipt.bytes == 93 && !encoder.Finished());
    Q4T_CHECK(!encoder.Append(std::string(2, '\0'), sink).ok());
  }
  RealtimeSpeechEncoder short_audio({RealtimeAudioEncoding::kMuLaw, 8000}, 1,
                                    1);
  const auto sink = [](std::string_view) { return ObjectReply{}; };
  Q4T_CHECK(short_audio.Append(std::string(2, '\0'), sink).ok());
  RealtimeSpeechReceipt receipt;
  Q4T_CHECK(!short_audio.Finish(sink, &receipt).ok());
  Q4T_CHECK(receipt.bytes == 0 && !short_audio.Finished());
  return true;
}

Q4T_TEST(realtime_speech_sink_failures_are_terminal_without_replay) {
  for (const int failure : {0, 1, 2, 3}) {
    RealtimeSpeechEncoder encoder({}, 5000, 10000);
    size_t calls = 0;
    const auto sink = [&](std::string_view) -> ObjectReply {
      if (++calls == 1) return {};
      ObjectReply error;
      if (failure == 0) error.code = "fixture_backpressure";
      if (failure == 1) error.commit_unknown = true;
      if (failure == 2) error.http_status = 503;
      if (failure == 3) throw std::runtime_error("fixture sink interrupted");
      return error;
    };
    const auto reply = encoder.Append(Pcm(5000), sink);
    Q4T_CHECK(!reply.ok());
    Q4T_CHECK(reply.commit_unknown == (failure == 1));
    Q4T_CHECK(calls == 2);
    RealtimeSpeechReceipt receipt{91, 92, 93};
    Q4T_CHECK(!encoder.Finish(sink, &receipt).ok());
    Q4T_CHECK(!encoder.Append(Pcm(1), sink).ok());
    Q4T_CHECK(calls == 2 && receipt.bytes == 93 && !encoder.Finished());
  }
  return true;
}

Q4T_TEST(realtime_speech_cancel_before_during_and_at_eof_preserves_failure) {
  for (const int mode : {0, 1, 2}) {
    bool cancel = mode == 0;
    RealtimeSpeechEncoder encoder({}, 4097, 8194, [&] { return cancel; });
    size_t calls = 0;
    const auto sink = [&](std::string_view) {
      ++calls;
      if (mode == 1) cancel = true;
      return ObjectReply{};
    };
    auto result = encoder.Append(Pcm(4097), sink);
    Q4T_CHECK(result.ok() == (mode == 2));
    if (mode == 0) Q4T_CHECK(calls == 0);
    if (mode == 1) Q4T_CHECK(calls == 1);
    cancel = true;
    const auto prior = calls;
    RealtimeSpeechReceipt receipt{91, 92, 93};
    Q4T_CHECK(!encoder.Finish(sink, &receipt).ok());
    Q4T_CHECK(calls == prior && receipt.bytes == 93 && !encoder.Finished());
  }
  return true;
}

Q4T_TEST(realtime_speech_invalid_config_and_final_state_never_emit) {
  for (const auto format :
       {RealtimeAudioFormat{RealtimeAudioEncoding::kPcm16, 48000},
        RealtimeAudioFormat{RealtimeAudioEncoding::kMuLaw, 24000},
        RealtimeAudioFormat{RealtimeAudioEncoding::kALaw, 0},
        RealtimeAudioFormat{RealtimeAudioEncoding::kFloat32, 24000}}) {
    RealtimeSpeechEncoder encoder(format, 8, 16);
    Q4T_CHECK(!encoder.Valid());
    size_t calls = 0;
    Q4T_CHECK(!encoder
                   .Append(Pcm(8),
                           [&](std::string_view) {
                             ++calls;
                             return ObjectReply{};
                           })
                   .ok());
    Q4T_CHECK(calls == 0);
  }
  Q4T_CHECK(!RealtimeSpeechEncoder({}, 0, 1).Valid());
  Q4T_CHECK(!RealtimeSpeechEncoder({}, 600ULL * 24000 + 1, 1).Valid());
  Q4T_CHECK(!RealtimeSpeechEncoder({}, 1, 0).Valid());
  Q4T_CHECK(!RealtimeSpeechEncoder({}, 1, kRealtimeAudioBytes + 1).Valid());
  RealtimeSpeechEncoder encoder({}, 2, 4);
  size_t calls = 0;
  const auto sink = [&](std::string_view) {
    ++calls;
    return ObjectReply{};
  };
  Q4T_CHECK(encoder.Append(Pcm(2), sink).ok());
  RealtimeSpeechReceipt receipt;
  Q4T_CHECK(encoder.Finish(sink, &receipt).ok());
  const auto prior = calls;
  Q4T_CHECK(!encoder.Finish(sink, &receipt).ok());
  Q4T_CHECK(!encoder.Append(Pcm(1), sink).ok());
  Q4T_CHECK(calls == prior && encoder.Finished());
  return true;
}

Q4T_TEST(live_speech_pcm16k_keeps_phase_and_exact_unpadded_tail) {
  const auto input = Pcm(24017);
  const RealtimeAudioFormat format{RealtimeAudioEncoding::kPcm16, 16000};
  std::string reference;
  Q4T_CHECK(Encode(input, format, input.size(), &reference));
  Q4T_CHECK(reference.size() == 16011 * 2);
  for (const size_t chunk : {1, 3, 17, 2047}) {
    std::string output;
    Q4T_CHECK(Encode(input, format, chunk, &output));
    Q4T_CHECK(output == reference);
  }
  return true;
}

Q4T_TEST(live_speech_pcm16k_preserves_dc_voice_and_rejects_alias) {
  const RealtimeAudioFormat format{RealtimeAudioEncoding::kPcm16, 16000};
  for (const double frequency : {0., 1000., 11000.}) {
    std::string input;
    for (size_t i = 0; i < 24000; ++i) {
      const auto linear =
          int16_t(frequency == 0
                      ? 8192
                      : std::lround(16384 * std::sin(2 * std::numbers::pi *
                                                     frequency * i / 24000)));
      input.push_back(char(uint16_t(linear) & 255));
      input.push_back(char(uint16_t(linear) >> 8));
    }
    std::string output;
    Q4T_CHECK(Encode(input, format, 373, &output));
    Q4T_CHECK(output.size() == 32000);
    double energy = 0;
    for (size_t i = 128; i + 128 < 16000; ++i) {
      const auto sample = DecodeRealtimeSample(
          RealtimeAudioEncoding::kPcm16, output[i * 2], output[i * 2 + 1]);
      if (frequency == 0) Q4T_CHECK(sample == .25f);
      energy += double(sample) * sample;
    }
    const auto rms = std::sqrt(energy / (16000 - 256));
    if (frequency == 1000) Q4T_CHECK(std::abs(rms - std::sqrt(.125)) < .001);
    if (frequency == 11000) Q4T_CHECK(rms < .0001);
  }
  return true;
}

Q4T_TEST(live_speech_pcm16k_budget_and_cancelled_tail_never_complete) {
  const auto input = Pcm(4097);
  const RealtimeAudioFormat format{RealtimeAudioEncoding::kPcm16, 16000};
  for (const size_t budget : {5461, 5462}) {
    RealtimeSpeechEncoder encoder(format, 4097, budget);
    std::string output;
    const auto sink = [&](std::string_view bytes) {
      output += bytes;
      return ObjectReply{};
    };
    const auto reply = encoder.Append(input, sink);
    RealtimeSpeechReceipt receipt{91, 92, 93};
    if (budget == 5461) {
      Q4T_CHECK(!reply.ok() && output.empty());
      Q4T_CHECK(!encoder.Finish(sink, &receipt).ok());
      Q4T_CHECK(receipt.bytes == 93);
    } else {
      Q4T_CHECK(reply.ok() && encoder.Finish(sink, &receipt).ok());
      Q4T_CHECK(receipt.input_samples == 4097 &&
                receipt.output_samples == 2731);
      Q4T_CHECK(receipt.bytes == 5462 && output.size() == 5462);
    }
  }
  bool cancelled = false;
  RealtimeSpeechEncoder encoder(format, 4097, 5462, [&] { return cancelled; });
  size_t bytes = 0;
  const auto sink = [&](std::string_view part) {
    bytes += part.size();
    return ObjectReply{};
  };
  Q4T_CHECK(encoder.Append(input, sink).ok());
  Q4T_CHECK(bytes > 0 && bytes < 5462);
  const auto accepted = bytes;
  cancelled = true;
  RealtimeSpeechReceipt receipt{91, 92, 93};
  Q4T_CHECK(!encoder.Finish(sink, &receipt).ok());
  Q4T_CHECK(!encoder.Finished() && bytes == accepted && receipt.bytes == 93);
  return true;
}
