#include "q4t/server/realtime_noise_input.h"

#include <algorithm>
#include <bit>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <vector>

#include "q4t/server/realtime_pcm.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::Status;
using q4t::audio::NoiseReduction;
std::vector<uint8_t> Encode(std::span<const float> samples) {
  std::vector<uint8_t> bytes;
  for (float value : samples) {
    const auto bits = std::bit_cast<uint32_t>(value);
    for (int b = 0; b < 4; ++b) bytes.push_back(uint8_t(bits >> (8 * b)));
  }
  return bytes;
}
std::vector<float> Wave(RealtimeAudioFormat format,
                        std::span<const uint8_t> raw) {
  RealtimeAudioQueue q(1, format);
  RealtimeAudioItem item;
  std::vector<float> result;
  if (!q.Append(1, raw) || !q.Commit(1, &item) ||
      !DecodeRealtimePcm(item, q.FrontBytes(), 160000, &result))
    throw std::runtime_error("fixture source decoder");
  return result;
}
std::vector<float> Filter(std::span<const float> values, NoiseReduction mode) {
  q4t::audio::NoiseStream stream(mode, values.size());
  std::array<float, 512> output;
  std::vector<float> result;
  for (size_t at = 0; at < values.size();) {
    q4t::audio::ResampleProgress p;
    if (!stream.Process(
            values.subspan(at, std::min<size_t>(512, values.size() - at)),
            output, &p))
      throw std::runtime_error("fixture filter");
    at += p.consumed;
    result.insert(result.end(), output.begin(), output.begin() + p.produced);
  }
  while (!stream.Drained()) {
    size_t count;
    if (!stream.Finish(output, &count))
      throw std::runtime_error("fixture tail");
    result.insert(result.end(), output.begin(), output.begin() + count);
  }
  return result;
}
}  // namespace

Q4T_TEST(realtime_noise_resample_overshoot_is_saturated_once_before_filter) {
  std::vector<uint8_t> raw(4096);
  for (size_t i = 0; i < raw.size() / 2; ++i) {
    const uint16_t value = (i / 97) % 2 ? 32767 : 32768;
    raw[2 * i] = uint8_t(value);
    raw[2 * i + 1] = uint8_t(value >> 8);
  }
  auto normalized = Wave({}, raw);
  size_t clipped = 0;
  float peak = 0;
  for (auto& value : normalized) {
    peak = std::max(peak, std::abs(value));
    clipped += std::abs(value) > 1;
    value = std::clamp(value, -1.0f, 1.0f);
  }
  Q4T_CHECK(clipped > 0 && peak > 1);
  std::fprintf(stderr, "Actual sinc overshoot: peak=%.9g clipped=%zu\n", peak,
               clipped);
  for (auto mode : {NoiseReduction::kNearField, NoiseReduction::kFarField}) {
    const auto expected = Filter(normalized, mode);
    for (size_t chunk : {1, 97, 4096}) {
      RealtimeNoiseInput stream({}, mode, 0);
      std::vector<float> actual;
      const auto sink = [&](auto values) {
        actual.insert(actual.end(), values.begin(), values.end());
        return Status{};
      };
      for (size_t at = 0; at < raw.size(); at += chunk)
        Q4T_CHECK(stream.Append(
            std::span(raw).subspan(at, std::min(chunk, raw.size() - at)),
            sink));
      Q4T_CHECK(stream.Finish(sink) && actual == expected);
      Q4T_CHECK(actual.size() == normalized.size());
      Q4T_CHECK(std::all_of(actual.begin(), actual.end(), [](float value) {
        return std::isfinite(value) && std::abs(value) <= 1;
      }));
    }
  }
  return true;
}

Q4T_TEST(realtime_noise_stream_matches_whole_reference_for_every_partition) {
  for (auto format : {RealtimeAudioFormat{RealtimeAudioEncoding::kPcm16, 24000},
                      {RealtimeAudioEncoding::kMuLaw, 8000},
                      {RealtimeAudioEncoding::kALaw, 8000}}) {
    const size_t width =
        format.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
    std::vector<uint8_t> raw((format.rate / 3 + 1) * width);
    for (size_t i = 0; i < raw.size(); ++i) raw[i] = uint8_t(i * 19 + 31);
    const auto original = Wave(format, raw);
    for (auto mode : {NoiseReduction::kNearField, NoiseReduction::kFarField}) {
      const auto expected = Filter(original, mode);
      for (size_t chunk : {1, 13, 4097}) {
        RealtimeNoiseInput stream(format, mode, 11);
        std::vector<float> actual;
        const auto sink = [&](auto values) {
          actual.insert(actual.end(), values.begin(), values.end());
          return Status{};
        };
        Q4T_CHECK(stream.Valid());
        for (size_t at = 0; at < raw.size(); at += chunk)
          Q4T_CHECK(stream.Append(
              std::span(raw).subspan(at, std::min(chunk, raw.size() - at)),
              sink));
        Q4T_CHECK(stream.Finish(sink));
        Q4T_CHECK(actual == expected && actual != original);
        Q4T_CHECK(stream.AcceptedTicks() ==
                  11 + raw.size() / width * (48000 / format.rate));
        Q4T_CHECK(stream.DeliveredSamples() == original.size());
        Q4T_CHECK(stream.Finish(sink) && actual == expected);
        Q4T_CHECK(!stream.Append({}, sink));
      }
    }
  }
  return true;
}

Q4T_TEST(realtime_noise_partial_word_and_fractional_clock_finish_exactly) {
  RealtimeNoiseInput stream({}, NoiseReduction::kNearField, 5);
  std::vector<float> output;
  const auto sink = [&](auto values) {
    output.insert(output.end(), values.begin(), values.end());
    return Status{};
  };
  const std::array<uint8_t, 1> low{17};
  Q4T_CHECK(stream.Append(low, sink) && stream.PartialSample());
  Q4T_CHECK(stream.AcceptedTicks() == 5 && !stream.Finish(sink));
  const std::array<uint8_t, 3> rest{0, 31, 0};
  Q4T_CHECK(stream.Append(rest, sink) && !stream.PartialSample());
  Q4T_CHECK(stream.AcceptedTicks() == 9 && stream.FinalSamples() == 1);
  Q4T_CHECK(stream.Finish(sink) && output.size() == 1);
  RealtimeNoiseInput next({}, NoiseReduction::kFarField,
                          stream.AcceptedTicks());
  Q4T_CHECK(next.AcceptedTicks() == 9 && next.DeliveredSamples() == 0);
  RealtimeNoiseInput invalid({RealtimeAudioEncoding::kPcm16, 0},
                             NoiseReduction::kOff, 0);
  Q4T_CHECK(!invalid.Valid() && invalid.FinalSamples() == 0);
  return true;
}

Q4T_TEST(realtime_noise_cancel_or_sink_failure_poison_only_owned_pipeline) {
  const std::vector<uint8_t> data(12000, 31);
  bool cancelled = false;
  RealtimeNoiseInput stream({}, NoiseReduction::kNearField, 0,
                            [&] { return cancelled; });
  size_t sent = 0;
  const auto sink = [&](auto values) {
    sent += values.size();
    cancelled = true;
    return Status{};
  };
  Q4T_CHECK(!stream.Append(data, sink) && sent > 0);
  cancelled = false;
  Q4T_CHECK(!stream.Finish(sink) && !stream.Append({}, sink));
  RealtimeNoiseInput failed({}, NoiseReduction::kFarField, 0);
  const auto fail = [](auto) { return Status::Fail("injected capacity"); };
  Q4T_CHECK(!failed.Append(data, fail) && !failed.Finish(fail));
  RealtimeNoiseInput healthy({}, NoiseReduction::kNearField, 0);
  Q4T_CHECK(healthy.Append(data, [](auto) { return Status{}; }));
  Q4T_CHECK(healthy.Finish([](auto) { return Status{}; }));
  return true;
}

Q4T_TEST(realtime_internal_float_queue_is_bit_exact_at_all_ring_offsets) {
  const std::array<float, 6> values{
      0, -.125f, 1, -1, std::numeric_limits<float>::denorm_min(), .123456789f};
  const auto bytes = Encode(values);
  for (size_t cycles = 0; cycles < 35; ++cycles) {
    RealtimeAudioQueue q(1, {RealtimeAudioEncoding::kFloat32, 16000}, 35);
    RealtimeAudioItem item;
    for (size_t i = 0; i < cycles; ++i) {
      Q4T_CHECK(q.Append(1, std::span(bytes).first(4)) && q.Commit(1, &item));
      Q4T_CHECK(q.Acknowledge(1, item.sequence));
    }
    Q4T_CHECK(q.Append(1, bytes) && q.Commit(1, &item));
    std::vector<float> decoded;
    Q4T_CHECK(DecodeRealtimePcm(item, q.FrontBytes(), 100, &decoded));
    Q4T_CHECK(std::equal(decoded.begin(), decoded.end(), values.begin(),
                         values.end()));
  }
  for (float bad : {1.01f, std::numeric_limits<float>::infinity(),
                    std::numeric_limits<float>::quiet_NaN()}) {
    const std::array<float, 1> value{bad};
    RealtimeAudioQueue q(1, {RealtimeAudioEncoding::kFloat32, 16000});
    RealtimeAudioItem item;
    Q4T_CHECK(q.Append(1, Encode(value)) && q.Commit(1, &item));
    std::vector<float> decoded{.5f};
    Q4T_CHECK(!DecodeRealtimePcm(item, q.FrontBytes(), 100, &decoded));
    Q4T_CHECK(decoded == std::vector<float>{.5f});
  }
  return true;
}

Q4T_TEST(
    realtime_empty_clock_advance_keeps_prior_borrow_and_new_sample_origin) {
  RealtimeAudioQueue q(1, {RealtimeAudioEncoding::kFloat32, 16000});
  const std::array<float, 2> values{.25f, .75f};
  const auto bytes = Encode(values);
  RealtimeAudioItem first, second;
  Q4T_CHECK(q.Append(1, bytes) && q.Commit(1, &first));
  const auto borrowed = q.FrontBytes();
  Q4T_CHECK(q.AdvanceEmptyClock(1, 11));
  Q4T_CHECK(q.Front()->end_ticks == 6 &&
            q.FrontBytes()[0].data() == borrowed[0].data());
  Q4T_CHECK(!q.AdvanceEmptyClock(1, 10));
  Q4T_CHECK(!q.AdvanceEmptyClock(1, kRealtimeSessionTicks + 1));
  Q4T_CHECK(q.Append(1, bytes) && !q.AdvanceEmptyClock(1, 20));
  Q4T_CHECK(q.Commit(1, &second));
  Q4T_CHECK(second.begin_ticks == 11 && second.end_ticks == 17 &&
            second.samples == 2);
  Q4T_CHECK(q.Acknowledge(1, first.sequence));
  Q4T_CHECK(q.Front()->begin_ticks == 11);
  return true;
}
