#include "q4t/server/realtime_vad.h"

#include <algorithm>
#include <bit>
#include <limits>
#include <stdexcept>

#include "q4t/server/realtime_pcm.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::Status;
std::vector<uint8_t> Signal(RealtimeAudioFormat format, size_t samples) {
  const bool pcm = format.encoding == RealtimeAudioEncoding::kPcm16;
  std::vector<uint8_t> bytes(samples * (pcm ? 2 : 1));
  for (size_t i = 0; i < samples; ++i) {
    const uint16_t value = uint16_t((i * 193) % 20000);
    bytes[i * (pcm ? 2 : 1)] = uint8_t(value);
    if (pcm) bytes[i * 2 + 1] = uint8_t(value >> 8);
  }
  return bytes;
}
std::vector<float> Frames(RealtimeAudioFormat format,
                          std::span<const uint8_t> data, size_t chunk) {
  RealtimeAudioQueue queue(1, format);
  std::vector<float> values;
  RealtimeVad vad(format, 0, {}, [&](auto frame, auto*, float* p) {
    values.insert(values.end(), frame.begin(), frame.end());
    *p = .7f;
    return Status{};
  });
  for (size_t at = 0; at < data.size(); at += chunk) {
    if (!queue.Append(1, data.subspan(at, std::min(chunk, data.size() - at))))
      throw std::runtime_error("fixture append");
    for (size_t loop = 0; loop < 100; ++loop) {
      const auto before = vad.CursorTicks();
      RealtimeVadProgress progress;
      if (!vad.Advance(queue, &progress))
        throw std::runtime_error("fixture advance");
      if (!progress.frame && before == vad.CursorTicks()) break;
      if (loop == 99) throw std::runtime_error("fixture loop bound");
    }
  }
  return values;
}
}  // namespace

Q4T_TEST(realtime_vad_processed_float_overshoot_is_bit_exact) {
  const RealtimeAudioFormat format{RealtimeAudioEncoding::kFloat32, 16000};
  std::vector<float> expected(1024);
  std::vector<uint8_t> bytes(expected.size() * 4);
  for (size_t i = 0; i < expected.size(); ++i) {
    expected[i] = i % 2 ? -1.00744641f : 1.00744641f;
    const auto bits = std::bit_cast<uint32_t>(expected[i]);
    for (size_t b = 0; b < 4; ++b) bytes[4 * i + b] = bits >> (8 * b);
  }
  for (size_t chunk : {1, 17, 513, 4096})
    Q4T_CHECK(Frames(format, bytes, chunk) == expected);
  return true;
}

Q4T_TEST(realtime_vad_processed_nonfinite_fails_before_callback) {
  const RealtimeAudioFormat format{RealtimeAudioEncoding::kFloat32, 16000};
  for (float invalid : {std::numeric_limits<float>::quiet_NaN(),
                        std::numeric_limits<float>::infinity(),
                        -std::numeric_limits<float>::infinity()}) {
    RealtimeAudioQueue queue(1, format);
    std::vector<uint8_t> bytes(512 * 4);
    const auto bits = std::bit_cast<uint32_t>(invalid);
    for (size_t b = 0; b < 4; ++b) bytes[4 * 511 + b] = bits >> (8 * b);
    Q4T_CHECK(queue.Append(1, bytes));
    int calls = 0;
    RealtimeVad vad(format, 0, {}, [&](auto, auto*, float* p) {
      ++calls;
      *p = 1;
      return Status{};
    });
    RealtimeVadProgress progress;
    Q4T_CHECK(!vad.Advance(queue, &progress));
    Q4T_CHECK(!vad.Advance(queue, &progress));
    Q4T_CHECK(calls == 0 && vad.CursorTicks() == 0 && !vad.Active());
  }
  return true;
}

Q4T_TEST(realtime_vad_continuous_frames_are_message_partition_invariant) {
  for (auto format : {RealtimeAudioFormat{RealtimeAudioEncoding::kPcm16, 8000},
                      {RealtimeAudioEncoding::kPcm16, 16000},
                      {RealtimeAudioEncoding::kPcm16, 24000},
                      {RealtimeAudioEncoding::kPcm16, 48000},
                      {RealtimeAudioEncoding::kMuLaw, 8000},
                      {RealtimeAudioEncoding::kALaw, 8000}}) {
    const auto bytes = Signal(format, format.rate / 5);
    const auto expected = Frames(format, bytes, bytes.size());
    Q4T_CHECK(expected.size() >= 5 * 512);
    for (size_t chunk : {1, 17, 513, 4096})
      Q4T_CHECK(Frames(format, bytes, chunk) == expected);
    RealtimeAudioQueue queue(1, format);
    RealtimeAudioItem item;
    Q4T_CHECK(queue.Append(1, bytes) && queue.Commit(1, &item));
    std::vector<float> whole;
    Q4T_CHECK(DecodeRealtimePcm(item, queue.FrontBytes(), 16000, &whole));
    Q4T_CHECK(std::equal(expected.begin(), expected.end(), whole.begin()));
  }
  return true;
}

Q4T_TEST(realtime_vad_sample_boundaries_include_prefix_and_exact_silence) {
  const RealtimeAudioFormat format{RealtimeAudioEncoding::kPcm16, 16000};
  RealtimeAudioQueue queue(1, format);
  Q4T_CHECK(queue.Append(1, Signal(format, 10 * 512)));
  const std::array<float, 10> probabilities{0, 0, 1, 1, 0, 0, 1, 0, 0, 0};
  size_t frame = 0;
  RealtimeVad vad(format, 0, {.prefix_ms = 16, .silence_ms = 48},
                  [&](auto, auto*, float* p) {
                    *p = probabilities.at(frame++);
                    return Status{};
                  });
  std::vector<RealtimeAudioItem> items;
  std::vector<uint64_t> starts;
  for (size_t i = 0; i < probabilities.size(); ++i) {
    RealtimeVadProgress p;
    Q4T_CHECK(vad.Advance(queue, &p) && p.frame);
    if (p.started) starts.push_back(p.begin_ticks);
    if (p.stopped) {
      RealtimeAudioItem item;
      Q4T_CHECK(queue.CommitPrefix(1, p.end_ticks, &item));
      items.push_back(item);
    } else
      Q4T_CHECK(queue.TrimPendingBefore(1, p.trim_before));
  }
  Q4T_CHECK(starts == std::vector<uint64_t>({768 * 3, 2816 * 3}));
  Q4T_CHECK(items.size() == 2 && items[0].begin_ticks == starts[0]);
  Q4T_CHECK(items[0].end_ticks == 2816 * 3);
  Q4T_CHECK(items[1].begin_ticks == items[0].end_ticks);
  Q4T_CHECK(items[1].end_ticks == 4352 * 3);
  // Gaps remain charged until the real borrower releases the corresponding
  // item; the speech bytes still begin at the declared exact sample.
  Q4T_CHECK(queue.FrontBytes()[0][0] == Signal(format, 769)[768 * 2]);
  Q4T_CHECK(queue.Acknowledge(1, items[0].sequence));
  Q4T_CHECK(queue.Front()->begin_ticks == items[1].begin_ticks);
  return true;
}

Q4T_TEST(realtime_vad_idle_ticks_do_not_invent_silence_or_finish) {
  const RealtimeAudioFormat format{RealtimeAudioEncoding::kPcm16, 16000};
  RealtimeAudioQueue q(1, format);
  int calls = 0;
  RealtimeVad vad(format, 0, {}, [&](auto, auto*, float* p) {
    ++calls;
    *p = 1;
    return Status{};
  });
  Q4T_CHECK(q.Append(1, Signal(format, 512 + 17)));
  RealtimeVadProgress p;
  Q4T_CHECK(vad.Advance(q, &p) && p.started && vad.Active());
  for (int i = 0; i < 100; ++i) {
    Q4T_CHECK(vad.Advance(q, &p));
    Q4T_CHECK(!p.frame && !p.stopped && vad.Active());
  }
  Q4T_CHECK(calls == 1 && vad.CursorTicks() == (512 + 17) * 3);
  Q4T_CHECK(q.Cancel(1) && !vad.Advance(q, &p));
  return true;
}

Q4T_TEST(realtime_vad_invalid_probability_poison_and_epoch_reset) {
  const RealtimeAudioFormat format{RealtimeAudioEncoding::kPcm16, 16000};
  RealtimeAudioQueue q(1, format);
  Q4T_CHECK(q.Append(1, Signal(format, 1024)));
  RealtimeVad bad(format, 0, {}, [](auto, auto*, float* p) {
    *p = std::numeric_limits<float>::quiet_NaN();
    return Status{};
  });
  RealtimeVadProgress p;
  Q4T_CHECK(!bad.Advance(q, &p));
  Q4T_CHECK(!bad.Advance(q, &p));
  Q4T_CHECK(q.Clear(1));
  const auto origin = q.AcceptedTicks();
  Q4T_CHECK(q.Append(1, Signal(format, 512)));
  RealtimeVad next(format, origin, {}, [](auto, auto* state, float* value) {
    if (state->context[0] != 0 || state->cell[0] != 0)
      return Status::Fail("fixture state leaked");
    *value = 1;
    return Status{};
  });
  Q4T_CHECK(next.Advance(q, &p) && p.started && p.begin_ticks == origin);
  Q4T_CHECK(q.AcceptedTicks() == origin + 512 * 3);
  RealtimeVad invalid(format, origin, {.threshold = 2}, {});
  Q4T_CHECK(!invalid.Valid());
  return true;
}
