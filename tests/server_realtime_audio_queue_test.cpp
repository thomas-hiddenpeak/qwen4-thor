#include "q4t/server/realtime_audio_queue.h"

#include <algorithm>
#include <limits>
#include <string>
#include <vector>

#include "q4t/test.h"

namespace {
using namespace q4t::server;
std::span<const uint8_t> Bytes(std::string_view text) {
  return {reinterpret_cast<const uint8_t*>(text.data()), text.size()};
}
std::string Front(const RealtimeAudioQueue& queue) {
  std::string output;
  for (auto part : queue.FrontBytes())
    if (!part.empty())
      output.append(reinterpret_cast<const char*>(part.data()), part.size());
  return output;
}
}  // namespace

Q4T_TEST(realtime_clock_exact_fraction_and_overflow_free_projection) {
  for (const uint64_t ticks : std::array<uint64_t, 7>{
           0, 1, 2, 3, 5, 6, std::numeric_limits<uint64_t>::max()}) {
    for (uint32_t rate : {8000, 16000, 24000, 48000}) {
      RealtimeSamplePosition position;
      Q4T_CHECK(ProjectRealtimeSamples(ticks, rate, &position));
      const auto step = kRealtimeTicksPerSecond / rate;
      Q4T_CHECK(position.sample == ticks / step);
      Q4T_CHECK(position.remainder_ticks == ticks % step);
    }
  }
  RealtimeSamplePosition original{33, 2}, position = original;
  Q4T_CHECK(!ProjectRealtimeSamples(600, 44100, &position));
  Q4T_CHECK(position == original);
  Q4T_CHECK(!ProjectRealtimeSamples(600, 16000, nullptr));
  return true;
}

Q4T_TEST(realtime_queue_partial_pcm_bytes_commit_atomically) {
  RealtimeAudioQueue queue(7, {}, 8);
  Q4T_CHECK(queue.Valid() && queue.StorageBytes() == 0);
  RealtimeAudioItem item, unchanged = item;
  Q4T_CHECK(queue.Append(7, Bytes("a")));
  Q4T_CHECK(queue.AcceptedTicks() == 0 && queue.PendingBytes() == 1);
  Q4T_CHECK(!queue.Commit(7, &item));
  Q4T_CHECK(item == unchanged && !queue.Front());
  Q4T_CHECK(queue.Append(7, Bytes("bc")));
  Q4T_CHECK(queue.AcceptedTicks() == 2 && queue.PendingBytes() == 3);
  Q4T_CHECK(!queue.Commit(7, &item));
  Q4T_CHECK(queue.Append(7, Bytes("d")));
  Q4T_CHECK(queue.Commit(7, &item));
  Q4T_CHECK(item.epoch == 7 && item.sequence == 1 && item.samples == 2);
  Q4T_CHECK(item.begin_ticks == 0 && item.end_ticks == 4 && item.bytes == 4);
  Q4T_CHECK(Front(queue) == "abcd" && queue.StorageBytes() == 8);
  Q4T_CHECK(queue.Acknowledge(7, item.sequence) && queue.UsedBytes() == 0);
  return true;
}

Q4T_TEST(realtime_queue_arbitrary_input_chunks_preserve_exact_clock) {
  const std::string data = "0123456789abcdef0123456789abcdef";
  for (size_t chunk = 1; chunk <= data.size(); ++chunk) {
    RealtimeAudioQueue queue(9, {}, data.size());
    for (size_t offset = 0; offset < data.size(); offset += chunk)
      Q4T_CHECK(queue.Append(
          9,
          Bytes(data).subspan(offset, std::min(chunk, data.size() - offset))));
    RealtimeAudioItem item;
    Q4T_CHECK(queue.Commit(9, &item));
    Q4T_CHECK(item.begin_ticks == 0 && item.end_ticks == 32);
    Q4T_CHECK(item.samples == 16 && Front(queue) == data);
    RealtimeSamplePosition asr;
    Q4T_CHECK(ProjectRealtimeSamples(item.end_ticks, 16000, &asr));
    Q4T_CHECK(asr.sample == 10 && asr.remainder_ticks == 2);
  }
  return true;
}

Q4T_TEST(realtime_queue_clear_preserves_committed_audio_and_sample_time) {
  RealtimeAudioQueue queue(11, {}, 16);
  RealtimeAudioItem a, b;
  Q4T_CHECK(queue.Append(11, Bytes("ab")) && queue.Commit(11, &a));
  const auto borrowed = queue.FrontBytes()[0];
  Q4T_CHECK(queue.Append(11, Bytes("cde")));
  Q4T_CHECK(queue.AcceptedTicks() == 4);
  Q4T_CHECK(queue.Clear(11));
  Q4T_CHECK(queue.AcceptedTicks() == 4 && queue.UsedBytes() == 2);
  Q4T_CHECK(Front(queue) == "ab" &&
            borrowed.data() == queue.FrontBytes()[0].data());
  Q4T_CHECK(queue.Append(11, Bytes("fg")) && queue.Commit(11, &b));
  Q4T_CHECK(b.begin_ticks == 4 && b.end_ticks == 6 && b.samples == 1);
  Q4T_CHECK(queue.Acknowledge(11, a.sequence));
  Q4T_CHECK(Front(queue) == "fg");
  return true;
}

Q4T_TEST(realtime_queue_ring_wrap_and_fifo_keep_borrowed_bytes_stable) {
  RealtimeAudioQueue queue(12, {}, 10);
  RealtimeAudioItem a, b, c;
  Q4T_CHECK(queue.Append(12, Bytes("abcdef")) && queue.Commit(12, &a));
  Q4T_CHECK(queue.Append(12, Bytes("gh")) && queue.Commit(12, &b));
  Q4T_CHECK(queue.Acknowledge(12, a.sequence));
  const auto borrowed = queue.FrontBytes()[0];
  Q4T_CHECK(queue.Append(12, Bytes("ijklmn")) && queue.Commit(12, &c));
  Q4T_CHECK(Front(queue) == "gh" &&
            borrowed.data() == queue.FrontBytes()[0].data());
  Q4T_CHECK(!queue.Acknowledge(12, c.sequence));
  Q4T_CHECK(queue.Acknowledge(12, b.sequence));
  Q4T_CHECK(queue.FrontBytes()[0].size() == 2 &&
            queue.FrontBytes()[1].size() == 4);
  Q4T_CHECK(Front(queue) == "ijklmn");
  Q4T_CHECK(queue.Acknowledge(12, c.sequence) && queue.UsedBytes() == 0);
  Q4T_CHECK(queue.AcceptedTicks() == 14);
  return true;
}

Q4T_TEST(realtime_queue_byte_and_duration_limits_do_not_mutate_state) {
  RealtimeAudioQueue queue(13, {}, 4, 2, 4);
  Q4T_CHECK(!queue.Append(13, Bytes("abcde")) && queue.StorageBytes() == 0);
  Q4T_CHECK(queue.Append(13, Bytes("abcd")));
  Q4T_CHECK(!queue.Append(13, Bytes("x")));
  Q4T_CHECK(queue.UsedBytes() == 4 && queue.AcceptedTicks() == 4);
  RealtimeAudioItem item;
  Q4T_CHECK(queue.Commit(13, &item) && queue.Acknowledge(13, item.sequence));
  Q4T_CHECK(!queue.Append(13, Bytes("ef")));
  Q4T_CHECK(queue.UsedBytes() == 0 && queue.AcceptedTicks() == 4);
  return true;
}

Q4T_TEST(realtime_queue_item_backpressure_retains_pending_input) {
  RealtimeAudioQueue queue(14, {}, 16, 1);
  RealtimeAudioItem a, b;
  Q4T_CHECK(queue.Append(14, Bytes("ab")) && queue.Commit(14, &a));
  Q4T_CHECK(queue.Append(14, Bytes("cd")));
  Q4T_CHECK(!queue.Commit(14, &b) && b.sequence == 0);
  Q4T_CHECK(queue.PendingBytes() == 2 && queue.Items() == 1);
  Q4T_CHECK(queue.Acknowledge(14, a.sequence));
  Q4T_CHECK(queue.Commit(14, &b) && b.sequence == 2 && Front(queue) == "cd");
  Q4T_CHECK(queue.PendingBytes() == 0 && queue.Items() == 1);
  return true;
}

Q4T_TEST(realtime_queue_stale_epoch_and_duplicate_completions_reject) {
  RealtimeAudioQueue queue(15, {}, 16);
  RealtimeAudioItem item;
  Q4T_CHECK(!queue.Append(14, Bytes("ab")) && queue.StorageBytes() == 0);
  Q4T_CHECK(queue.Append(15, Bytes("ab")) && queue.Commit(15, &item));
  Q4T_CHECK(!queue.Clear(14) && !queue.Cancel(14) && !queue.FinishInput(14));
  Q4T_CHECK(!queue.Acknowledge(14, item.sequence));
  Q4T_CHECK(!queue.Acknowledge(15, item.sequence + 1));
  Q4T_CHECK(Front(queue) == "ab" && !queue.Cancelled());
  Q4T_CHECK(queue.Acknowledge(15, item.sequence));
  Q4T_CHECK(!queue.Acknowledge(15, item.sequence));
  return true;
}

Q4T_TEST(realtime_queue_cancellation_keeps_worker_storage_until_ack) {
  RealtimeAudioQueue queue(16, {}, 16);
  RealtimeAudioItem item, ignored;
  Q4T_CHECK(queue.Append(16, Bytes("ab")) && queue.Commit(16, &item));
  const auto borrowed = queue.FrontBytes()[0];
  Q4T_CHECK(queue.Append(16, Bytes("cde")) && queue.Cancel(16));
  Q4T_CHECK(queue.Cancelled() && !queue.Drained() && queue.PendingBytes() == 0);
  Q4T_CHECK(queue.UsedBytes() == 2 && queue.AcceptedTicks() == 4);
  Q4T_CHECK(Front(queue) == "ab" &&
            borrowed.data() == queue.FrontBytes()[0].data());
  Q4T_CHECK(!queue.Append(16, Bytes("f")) && !queue.Commit(16, &ignored));
  Q4T_CHECK(queue.Cancel(16) && queue.UsedBytes() == 2);
  Q4T_CHECK(queue.Acknowledge(16, item.sequence) && queue.Drained());
  Q4T_CHECK(queue.Cancelled());
  return true;
}

Q4T_TEST(realtime_queue_normal_drain_requires_explicit_tail_resolution) {
  RealtimeAudioQueue queue(17, {}, 16);
  RealtimeAudioItem item;
  Q4T_CHECK(queue.Append(17, Bytes("a")));
  Q4T_CHECK(!queue.FinishInput(17) && !queue.Drained());
  Q4T_CHECK(queue.Append(17, Bytes("b")) && queue.Commit(17, &item));
  Q4T_CHECK(queue.FinishInput(17) && !queue.Drained());
  Q4T_CHECK(!queue.Append(17, {}) && !queue.FinishInput(17));
  Q4T_CHECK(queue.Acknowledge(17, item.sequence) && queue.Drained());
  Q4T_CHECK(!queue.Cancelled());
  return true;
}

Q4T_TEST(realtime_queue_format_change_keeps_session_clock_and_requires_drain) {
  RealtimeAudioQueue queue(18, {}, 16);
  const RealtimeAudioFormat mulaw{RealtimeAudioEncoding::kMuLaw, 8000};
  RealtimeAudioItem a, b;
  Q4T_CHECK(queue.Append(18, Bytes("ab")));
  Q4T_CHECK(!queue.ChangeFormat(18, mulaw));
  Q4T_CHECK(queue.Commit(18, &a));
  Q4T_CHECK(!queue.ChangeFormat(18, mulaw));
  Q4T_CHECK(queue.Acknowledge(18, a.sequence) && queue.ChangeFormat(18, mulaw));
  Q4T_CHECK(queue.Append(18, Bytes("x")) && queue.Commit(18, &b));
  Q4T_CHECK(b.format == mulaw && b.samples == 1);
  Q4T_CHECK(b.begin_ticks == 2 && b.end_ticks == 8);
  RealtimeSamplePosition begin, end;
  Q4T_CHECK(ProjectRealtimeSamples(b.begin_ticks, 16000, &begin));
  Q4T_CHECK(ProjectRealtimeSamples(b.end_ticks, 16000, &end));
  Q4T_CHECK(begin.sample == 0 && begin.remainder_ticks == 2);
  Q4T_CHECK(end.sample == 2 && end.remainder_ticks == 2);
  return true;
}

Q4T_TEST(realtime_queue_invalid_limits_and_repeated_ring_cycles) {
  for (const auto bytes : {size_t{0}, size_t{1}, kRealtimeAudioBytes + 1}) {
    RealtimeAudioQueue queue(19, {}, bytes);
    Q4T_CHECK(!queue.Valid() && !queue.Append(19, Bytes("ab")));
    Q4T_CHECK(queue.StorageBytes() == 0);
  }
  Q4T_CHECK(!RealtimeAudioQueue(0).Valid());
  Q4T_CHECK(!RealtimeAudioQueue(19, {}, 8, 0).Valid());
  Q4T_CHECK(!RealtimeAudioQueue(19, {}, 8, 33).Valid());
  Q4T_CHECK(
      !RealtimeAudioQueue(19, {}, 8, 1, kRealtimeSessionTicks + 1).Valid());
  Q4T_CHECK(
      !RealtimeAudioQueue(19, {RealtimeAudioEncoding::kALaw, 24000}).Valid());
  RealtimeAudioQueue queue(19, {}, 10, 2);
  for (uint64_t n = 1; n <= 100; ++n) {
    RealtimeAudioItem item;
    Q4T_CHECK(queue.Append(19, Bytes("abcdefgh")) && queue.Commit(19, &item));
    Q4T_CHECK(item.sequence == n && Front(queue) == "abcdefgh");
    Q4T_CHECK(item.end_ticks == n * 8 && queue.StorageBytes() == 10);
    Q4T_CHECK(queue.Acknowledge(19, n));
  }
  Q4T_CHECK(queue.UsedBytes() == 0 && queue.Items() == 0);
  return true;
}
