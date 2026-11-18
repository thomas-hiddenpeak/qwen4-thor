#include "q4t/server/realtime_pcm.h"

#include <algorithm>
#include <bit>
#include <cmath>
#include <limits>
#include <string>

#include "q4t/test.h"

namespace {
using namespace q4t::server;
#include "server_realtime_pcm_g711.inc"

RealtimeAudioItem Item(size_t bytes, RealtimeAudioFormat format = {}) {
  const auto samples =
      bytes / (format.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1);
  return {17,      3,     960,   960 + samples * (48000 / format.rate),
          samples, bytes, format};
}
std::array<std::span<const uint8_t>, 2> Spans(std::span<const uint8_t> bytes,
                                              size_t split) {
  return {bytes.first(split), bytes.subspan(split)};
}
bool Same(std::span<const float> a, std::span<const float> b) {
  if (a.size() != b.size()) return false;
  for (size_t i = 0; i < a.size(); ++i)
    if (std::bit_cast<uint32_t>(a[i]) != std::bit_cast<uint32_t>(b[i]))
      return false;
  return true;
}
}  // namespace

Q4T_TEST(realtime_pcm16_all_values_keep_signed_bits_across_odd_wrap) {
  std::vector<uint8_t> bytes(65536 * 2);
  for (unsigned word = 0; word < 65536; ++word) {
    bytes[word * 2] = word & 255;
    bytes[word * 2 + 1] = word >> 8;
  }
  auto item = Item(bytes.size(), {RealtimeAudioEncoding::kPcm16, 16000});
  std::vector<float> pcm;
  Q4T_CHECK(DecodeRealtimePcm(item, Spans(bytes, 65537), 65536, &pcm));
  Q4T_CHECK(pcm.size() == 65536);
  for (unsigned word = 0; word < 65536; ++word) {
    const float expected =
        float(word >= 32768 ? int(word) - 65536 : int(word)) / 32768.0f;
    Q4T_CHECK(std::bit_cast<uint32_t>(pcm[word]) ==
              std::bit_cast<uint32_t>(expected));
  }
  return true;
}

Q4T_TEST(realtime_g711_all_codes_match_independent_decoder_unity_dc) {
  for (const auto encoding :
       {RealtimeAudioEncoding::kMuLaw, RealtimeAudioEncoding::kALaw}) {
    const auto& reference =
        encoding == RealtimeAudioEncoding::kMuLaw ? kMuLaw : kALaw;
    for (unsigned code = 0; code < 256; ++code) {
      const std::vector<uint8_t> bytes(128, code);
      const auto item = Item(bytes.size(), {encoding, 8000});
      std::vector<float> pcm;
      Q4T_CHECK(DecodeRealtimePcm(item, Spans(bytes, 37), 256, &pcm));
      Q4T_CHECK(pcm.size() == 256);
      const float expected = float(reference[code]) / 32768.0f;
      // Both sub-sample phases are inside the constant part of the signal,
      // away from the centered filter's zero-extended boundaries.
      Q4T_CHECK(std::abs(pcm[128] - expected) <= 1e-7f);
      Q4T_CHECK(std::abs(pcm[129] - expected) <= 1e-7f);
    }
  }
  return true;
}

Q4T_TEST(realtime_pcm_every_queue_split_preserves_phase_and_complete_output) {
  for (const auto format :
       {RealtimeAudioFormat{},
        RealtimeAudioFormat{RealtimeAudioEncoding::kMuLaw, 8000},
        RealtimeAudioFormat{RealtimeAudioEncoding::kALaw, 8000}}) {
    std::vector<uint8_t> bytes(
        format.encoding == RealtimeAudioEncoding::kPcm16 ? 514 : 257);
    for (size_t i = 0; i < bytes.size(); ++i) bytes[i] = (i * 113 + 37) % 256;
    const auto item = Item(bytes.size(), format);
    std::vector<float> baseline;
    Q4T_CHECK(DecodeRealtimePcm(item, Spans(bytes, 0), 1024, &baseline));
    for (size_t split = 0; split <= bytes.size(); ++split) {
      std::vector<float> actual;
      Q4T_CHECK(DecodeRealtimePcm(item, Spans(bytes, split), 1024, &actual));
      Q4T_CHECK(Same(actual, baseline));
    }
    Q4T_CHECK(baseline.size() == item.samples * 16000 / format.rate);
  }
  return true;
}

Q4T_TEST(realtime_pcm_invalid_metadata_and_spans_leave_output_unchanged) {
  const std::vector<uint8_t> bytes(512, 0);
  const auto good = Item(bytes.size());
  for (int bad = 0; bad < 12; ++bad) {
    auto item = good;
    auto spans = Spans(bytes, 73);
    switch (bad) {
      case 0:
        item.epoch = 0;
        break;
      case 1:
        item.sequence = 0;
        break;
      case 2:
        --item.samples;
        break;
      case 3:
        --item.bytes;
        break;
      case 4:
        --item.end_ticks;
        break;
      case 5:
        item.begin_ticks = item.end_ticks + 1;
        break;
      case 6:
        item.format.rate = 0;
        break;
      case 7:
        item.format.rate = 44100;
        break;
      case 8:
        item.format.encoding = RealtimeAudioEncoding(-1);
        break;
      case 9:
        spans[1] = spans[1].subspan(1);
        break;
      case 10:
        item.samples = SIZE_MAX;
        break;
      case 11:
        item.end_ticks = UINT64_MAX;
        break;
    }
    std::vector<float> output{123.0f};
    int callback = 0;
    Q4T_CHECK(!DecodeRealtimePcm(item, spans, 512, &output, [&] {
      ++callback;
      return false;
    }));
    Q4T_CHECK(output == std::vector<float>{123.0f} && callback == 0);
  }
  Q4T_CHECK(!DecodeRealtimePcm(good, Spans(bytes, 0), 512, nullptr));
  return true;
}

Q4T_TEST(realtime_pcm_sample_budget_and_short_tail_never_pad_audio) {
  for (const uint32_t rate : {8000, 16000, 24000, 48000}) {
    for (size_t count = 1; count < 140; ++count) {
      const std::vector<uint8_t> bytes(count * 2, 0);
      const auto item =
          Item(bytes.size(), {RealtimeAudioEncoding::kPcm16, rate});
      const auto expected = count * 16000 / rate;
      std::vector<float> pcm{123.0f};
      const auto decoded = DecodeRealtimePcm(item, Spans(bytes, 1), 512, &pcm);
      if (expected) {
        Q4T_CHECK(decoded && pcm.size() == expected);
        Q4T_CHECK(std::all_of(pcm.begin(), pcm.end(),
                              [](float x) { return x == 0; }));
        if (expected > 1) {
          pcm = {123.0f};
          Q4T_CHECK(
              !DecodeRealtimePcm(item, Spans(bytes, 0), expected - 1, &pcm));
          Q4T_CHECK(pcm == std::vector<float>{123.0f});
        }
      } else {
        Q4T_CHECK(!decoded && pcm == std::vector<float>{123.0f});
      }
    }
  }
  const std::vector<uint8_t> bytes(8, 0);
  for (const size_t invalid : {size_t{0}, size_t{600 * 16000 + 1}, SIZE_MAX}) {
    std::vector<float> output{123.0f};
    Q4T_CHECK(!DecodeRealtimePcm(Item(bytes.size()), Spans(bytes, 1), invalid,
                                 &output));
    Q4T_CHECK(output == std::vector<float>{123.0f});
  }
  return true;
}

Q4T_TEST(realtime_pcm_cancel_before_during_and_after_decode_is_transactional) {
  const std::vector<uint8_t> bytes(4096 * 2, 17);
  const auto item = Item(bytes.size());
  for (const int cancel_at : {1, 3, 6}) {
    int calls = 0;
    std::vector<float> output{123.0f};
    const auto status = DecodeRealtimePcm(item, Spans(bytes, 37), 4096, &output,
                                          [&] { return ++calls == cancel_at; });
    Q4T_CHECK(!status && calls == cancel_at);
    Q4T_CHECK(output == std::vector<float>{123.0f});
  }
  return true;
}

Q4T_TEST(realtime_pcm_committed_queue_wrap_retains_borrow_until_exact_ack) {
  RealtimeAudioQueue queue(17, {}, 1024, 4);
  Q4T_CHECK(queue.Valid());
  std::vector<uint8_t> first(300, 7), second(300, 19), last(600);
  for (size_t i = 0; i < last.size(); ++i) last[i] = i % 256;
  RealtimeAudioItem a, b, c;
  Q4T_CHECK(queue.Append(17, first) && queue.Commit(17, &a));
  Q4T_CHECK(queue.Append(17, second) && queue.Commit(17, &b));
  Q4T_CHECK(queue.Acknowledge(17, a.sequence));
  Q4T_CHECK(queue.Append(17, last) && queue.Commit(17, &c));
  Q4T_CHECK(queue.Acknowledge(17, b.sequence));
  const auto borrowed = queue.FrontBytes();
  Q4T_CHECK(!borrowed[0].empty() && !borrowed[1].empty());
  const auto ticks = queue.AcceptedTicks();
  Q4T_CHECK(queue.FinishInput(17) && !queue.Drained());
  std::vector<float> actual, expected;
  Q4T_CHECK(DecodeRealtimePcm(c, borrowed, 512, &actual));
  Q4T_CHECK(DecodeRealtimePcm(c, Spans(last, 0), 512, &expected));
  Q4T_CHECK(Same(actual, expected) && actual.size() == 200);
  Q4T_CHECK(queue.FrontBytes()[0].data() == borrowed[0].data());
  Q4T_CHECK(queue.AcceptedTicks() == ticks && queue.UsedBytes() == last.size());
  Q4T_CHECK(queue.Acknowledge(17, c.sequence) && queue.Drained());
  return true;
}
