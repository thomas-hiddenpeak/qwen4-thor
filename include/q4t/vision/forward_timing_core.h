// Request-local event accounting; the backend owns only event API operations.
// No synchronization or CUDA dependency here. Collection requires completion
// at the caller's existing boundary. Missing/failed events are never zero time.
#pragma once

#include <array>
#include <cmath>
#include <cstddef>
#include <cstdio>
#include <vector>

namespace q4t::vision {

enum class ForwardStage {
  kPatch,
  kPositionTables,
  kPositionEmbed,
  kNorm,
  kQkv,
  kRope,
  kAttention,
  kProjection,
  kMlp,
  kMerger,
  kCount
};

inline constexpr std::array<const char*,
                            static_cast<size_t>(ForwardStage::kCount)>
    kForwardNames{
        "patch", "position_tables", "position_embed", "norm", "qkv",
        "rope",  "attention",       "projection",     "mlp",  "merger"};

template <typename Backend>
class ForwardEventTiming {
 public:
  using Event = typename Backend::Event;
  using Stream = typename Backend::Stream;
  static constexpr size_t kMaxEvents = 8192;
  explicit ForwardEventTiming(bool enabled, Backend backend = {})
      : enabled_(enabled), backend_(backend) {}
  ~ForwardEventTiming() {
    // CUDA permits destroying a pending event without waiting; destruction
    // does not release any compute buffers. The CUDA backend checks failures.
    for (size_t i = 0; i < created_; ++i) backend_.Destroy(events_[i]);
  }
  ForwardEventTiming(const ForwardEventTiming&) = delete;
  ForwardEventTiming& operator=(const ForwardEventTiming&) = delete;

  bool Enabled() const { return enabled_; }
  bool Valid() const { return collected_ && error_ == nullptr; }
  int ErrorCode() const { return error_code_; }
  const char* Error() const { return error_ ? error_ : "incomplete"; }
  size_t Recorded() const { return recorded_; }
  double TotalMs() const { return total_ms_; }
  double OtherMs() const { return other_ms_; }
  double Milliseconds(ForwardStage stage) const {
    return milliseconds_[static_cast<size_t>(stage)];
  }
  size_t Calls(ForwardStage stage) const {
    return calls_[static_cast<size_t>(stage)];
  }

  bool Prepare(size_t items, int depth) {
    if (!enabled_) return true;
    // Each item has position tables/embed, merger, and seven block sections.
    // Patch embed covers the concatenated batch. Two endpoints per section,
    // plus two for the whole span. Check before multiplication/allocation.
    constexpr size_t kMaxIntervals = (kMaxEvents - 2) / 2;
    if (error_ || !events_.empty() || items == 0 || depth <= 0 ||
        static_cast<size_t>(depth) > (kMaxIntervals - 3) / 7)
      return Fail("capacity_or_reuse");
    const size_t per_item = 3 + 7 * static_cast<size_t>(depth);
    if (items > (kMaxIntervals - 1) / per_item) return Fail("capacity");
    const size_t intervals = 1 + items * per_item;
    events_.resize(2 + 2 * intervals);
    stages_.reserve(intervals);
    for (auto& event : events_) {
      if (!Check(backend_.Create(&event), "create")) return false;
      ++created_;
    }
    return true;
  }

  bool Start(Stream stream) {
    if (!enabled_) return true;
    if (error_ || created_ != events_.size() || !created_ || recorded_)
      return Fail("start_state");
    stream_ = stream;
    return Mark();
  }
  bool Begin(ForwardStage stage) {
    if (!enabled_) return true;
    if (error_ || !recorded_ || finished_ || open_ ||
        static_cast<size_t>(stage) >= kForwardNames.size())
      return Fail("begin_state");
    if (!Mark()) return false;
    stages_.push_back(stage);
    open_ = true;
    return true;
  }
  bool End() {
    if (!enabled_) return true;
    if (error_ || !open_) return Fail("end_state");
    if (!Mark()) return false;
    open_ = false;
    return true;
  }
  bool Finish() {
    if (!enabled_) return true;
    if (error_ || open_ || finished_ || recorded_ + 1 != events_.size())
      return Fail("incomplete");
    if (!Mark()) return false;
    finished_ = true;
    return true;
  }

  bool CollectAfterCompletion() {
    if (!enabled_) return true;
    if (error_ || !finished_ || collected_) return Fail("collect_state");
    // Absolute offsets from the same origin preserve the partition even when
    // float event durations round. Do not add independently rounded pair times.
    double previous = 0, begin = 0;
    for (size_t i = 1; i < recorded_; ++i) {
      float offset = 0;
      if (!Check(backend_.Elapsed(&offset, events_[0], events_[i]), "elapsed"))
        return false;
      if (!std::isfinite(offset) || offset < previous)
        return Fail("nonmonotonic_or_nonfinite");
      if (i == recorded_ - 1) {
        total_ms_ = offset;
      } else if (i % 2) {
        begin = offset;
      } else {
        const size_t stage = static_cast<size_t>(stages_[i / 2 - 1]);
        milliseconds_[stage] += offset - begin;
        ++calls_[stage];
      }
      previous = offset;
    }
    double measured = 0;
    for (double value : milliseconds_) measured += value;
    other_ms_ = total_ms_ - measured;
    if (total_ms_ <= 0 || other_ms_ < 0) return Fail("invalid_partition");
    collected_ = true;
    return true;
  }

  void Report(FILE* sink, const char* id, size_t items, size_t patches,
              int depth, double setup_ms, double read_ms,
              double position_host_ms) const {
    if (!enabled_) return;
    if (!Valid()) {
      std::fprintf(sink,
                   "[q4t][vision-forward-timing] version=1 id=%s valid=0 "
                   "error=%s error_code=%d events=%zu\n",
                   id, Error(), error_code_, recorded_);
      return;
    }
    char fields[1536];
    size_t offset = 0;
    for (size_t i = 0; i < kForwardNames.size(); ++i) {
      const int n = std::snprintf(
          fields + offset, sizeof(fields) - offset, " %s_ms=%.6f %s_calls=%zu",
          kForwardNames[i], milliseconds_[i], kForwardNames[i], calls_[i]);
      if (n < 0 || static_cast<size_t>(n) >= sizeof(fields) - offset) return;
      offset += static_cast<size_t>(n);
    }
    std::fprintf(sink,
                 "[q4t][vision-forward-timing] version=1 id=%s valid=1 "
                 "clock=cuda_stream items=%zu patches=%zu depth=%d events=%zu "
                 "total_ms=%.6f other_ms=%.6f setup_host_ms=%.6f "
                 "read_host_ms=%.6f position_host_ms=%.6f%s\n",
                 id, items, patches, depth, recorded_, total_ms_, other_ms_,
                 setup_ms, read_ms, position_host_ms, fields);
  }

 private:
  bool Fail(const char* error) {
    if (!error_) error_ = error;
    return false;
  }
  bool Check(int code, const char* error) {
    if (code == 0) return true;
    if (!error_) error_code_ = code;
    return Fail(error);
  }
  bool Mark() {
    if (recorded_ >= created_) return Fail("event_capacity");
    if (!Check(backend_.Record(events_[recorded_], stream_), "record"))
      return false;
    ++recorded_;
    return true;
  }

  const bool enabled_;
  Backend backend_;
  std::vector<Event> events_;
  std::vector<ForwardStage> stages_;
  size_t created_ = 0;
  size_t recorded_ = 0;
  Stream stream_{};
  bool open_ = false;
  bool finished_ = false;
  bool collected_ = false;
  const char* error_ = nullptr;
  int error_code_ = 0;
  std::array<double, kForwardNames.size()> milliseconds_{};
  std::array<size_t, kForwardNames.size()> calls_{};
  double total_ms_ = 0;
  double other_ms_ = 0;
};

}  // namespace q4t::vision
