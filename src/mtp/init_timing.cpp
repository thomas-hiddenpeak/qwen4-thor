#include "q4t/mtp/init_timing.h"

#include <cmath>
#include <cstdio>
#include <iomanip>
#include <sstream>
#include <utility>

namespace q4t::mtp {
namespace {
void Quote(std::ostream& out, const char* value) {
  out << '"';
  for (const unsigned char* p = reinterpret_cast<const unsigned char*>(value);
       *p; ++p) {
    if (*p == '"' || *p == '\\')
      out << '\\' << static_cast<char>(*p);
    else if (*p < 32) {
      constexpr char kHex[] = "0123456789abcdef";
      out << "\\u00" << kHex[*p >> 4] << kHex[*p & 15];
    } else
      out << static_cast<char>(*p);
  }
  out << '"';
}
}  // namespace

MtpInitTiming::MtpInitTiming(std::string response_id, int rows, int chunk_size,
                             size_t trunk_bytes, Clock::time_point origin)
    : response_id_(std::move(response_id)),
      rows_(rows),
      chunk_size_(chunk_size),
      trunk_bytes_(trunk_bytes),
      origin_(origin) {
  const auto begin = Clock::now();
  MarkHost("timing_setup_begin");
  if (rows <= 0 || chunk_size <= 0) {
    Fail("invalid_shape");
  } else {
    const size_t count = (static_cast<size_t>(rows) - 1) / chunk_size + 1;
    if (count > kMaxChunks)
      Fail("chunk_capacity");
    else {
      // Allocate all host storage before creating any CUDA events.
      chunks_.resize(count);
      for (auto& event : outer_) Create(event);
      for (auto& chunk : chunks_)
        for (auto& event : chunk.events) Create(event);
    }
  }
  MarkHost("timing_setup_end");
  setup_host_ms_ =
      std::chrono::duration<double, std::milli>(Clock::now() - begin).count();
}

MtpInitTiming::~MtpInitTiming() noexcept {
  MarkHost("request_end");
  // Cleanup precedes publication so a destroy error also invalidates the one
  // record. Query/elapsed happen before destroy, and never wait for completion.
  try {
    Report();
  } catch (...) {
    std::fprintf(stderr,
                 "[q4t][mtp_init_timing] {\"schema_version\":1,"
                 "\"response_id\":\"%s\",\"valid\":false,"
                 "\"complete\":false,\"error\":\"report_exception\"}\n",
                 response_id_.c_str());
  }
  for (auto& event : outer_)
    if (event.handle) cudaEventDestroy(event.handle);
  for (auto& chunk : chunks_)
    for (auto& event : chunk.events)
      if (event.handle) cudaEventDestroy(event.handle);
}

double MtpInitTiming::NowMs() const noexcept {
  return std::chrono::duration<double, std::milli>(Clock::now() - origin_)
      .count();
}
void MtpInitTiming::Fail(const char* reason) noexcept {
  if (!error_) error_ = reason;
}
void MtpInitTiming::Create(Event& event) noexcept {
  if (!error_ && cudaEventCreate(&event.handle) != cudaSuccess)
    Fail("event_create");
}
void MtpInitTiming::Record(Event& event, cudaStream_t stream) noexcept {
  if (event.recorded) {
    Fail("duplicate_event");
    return;
  }
  event.host_ms = NowMs();
  if (error_ || !event.handle) return;
  const auto begin = Clock::now();
  const auto status = cudaEventRecord(event.handle, stream);
  record_host_ms_ +=
      std::chrono::duration<double, std::milli>(Clock::now() - begin).count();
  if (status != cudaSuccess)
    Fail("event_record");
  else
    event.recorded = true;
}
void MtpInitTiming::MarkHost(const char* name, int base, int rows) noexcept {
  if (host_count_ == host_.size()) {
    Fail("host_capacity");
    return;
  }
  host_[host_count_++] = {name, NowMs(), base, rows};
}
void MtpInitTiming::MarkOuter(InitOuterPoint point,
                              cudaStream_t stream) noexcept {
  Record(outer_[static_cast<size_t>(point)], stream);
}
void MtpInitTiming::BeginChunk(int base, int rows, bool compute_logits,
                               bool last_row, cudaStream_t stream,
                               bool tail_skipped) noexcept {
  if (chunk_count_ == chunks_.size()) {
    Fail("chunk_capacity");
    return;
  }
  auto& chunk = chunks_[chunk_count_++];
  chunk.base = base;
  chunk.rows = rows;
  chunk.compute_logits = compute_logits;
  chunk.last_row = last_row;
  chunk.tail_skipped = tail_skipped;
  if (tail_skipped &&
      (compute_logits || base < 0 || rows != chunk_size_ || rows >= rows_ ||
       base >= rows_ - rows))
    Fail("tail_skip_scope");
  MarkGpu(InitGpuPoint::kInputBegin, stream);
}
void MtpInitTiming::MarkGpu(InitGpuPoint point, cudaStream_t stream) noexcept {
  if (chunk_count_ == 0 || chunk_count_ > chunks_.size()) {
    Fail("chunk_missing");
    return;
  }
  auto& chunk = chunks_[chunk_count_ - 1];
  const size_t index = static_cast<size_t>(point);
  if (index != chunk.next_point || index >= kPoints) {
    Fail("event_order");
    return;
  }
  ++chunk.next_point;
  Record(chunk.events[index], stream);
}
void MtpInitTiming::FinishFirstContent() noexcept {
  if (!first_content_written_) {
    first_content_written_ = true;
    MarkHost("first_content_write_end");
  }
}

void MtpInitTiming::Report() {
  const auto report_begin = Clock::now();
  std::ostringstream body;
  body << std::setprecision(17) << std::boolalpha;
  struct Measurement {
    bool recorded;
    bool ready;
    float elapsed;
  };
  auto measure = [&](Event& begin, Event& end) {
    const bool recorded = begin.recorded && end.recorded;
    bool ready = false;
    float elapsed = 0;
    if (recorded) {
      const auto a = cudaEventQuery(begin.handle);
      const auto b = cudaEventQuery(end.handle);
      ready = a == cudaSuccess && b == cudaSuccess;
      if (!ready)
        Fail(a == cudaErrorNotReady || b == cudaErrorNotReady
                 ? "event_not_ready"
                 : "event_query");
      if (ready && cudaEventElapsedTime(&elapsed, begin.handle, end.handle) !=
                       cudaSuccess) {
        ready = false;
        Fail("event_elapsed");
      }
      if (ready && (!std::isfinite(elapsed) || elapsed < 0)) {
        ready = false;
        Fail("event_nonfinite");
      }
    } else
      Fail("event_missing");
    return Measurement{recorded, ready, elapsed};
  };
  auto span = [&](const char* name, Event& begin, Event& end, bool skipped) {
    const auto result = measure(begin, end);
    body << "{\"name\":";
    Quote(body, name);
    body << ",\"begin_host_ms\":";
    if (begin.host_ms >= 0)
      body << begin.host_ms;
    else
      body << "null";
    body << ",\"end_host_ms\":";
    if (end.host_ms >= 0)
      body << end.host_ms;
    else
      body << "null";
    body << ",\"stream_ms\":";
    if (result.ready && !skipped)
      body << result.elapsed;
    else
      body << "null";
    body << ",\"recorded\":" << result.recorded << ",\"ready\":" << result.ready
         << ",\"skipped\":" << skipped << '}';
    return result;
  };
  body << ",\"host_marks\":[";
  for (size_t i = 0; i < host_count_; ++i) {
    if (i) body << ',';
    const auto& mark = host_[i];
    body << "{\"name\":";
    Quote(body, mark.name);
    body << ",\"at_ms\":" << mark.at_ms << ",\"base\":" << mark.base
         << ",\"rows\":" << mark.rows << '}';
  }
  body << "],\"outer\":[";
  span("main_prefill", outer_[0], outer_[1], false);
  body << ',';
  span("draft_reset", outer_[2], outer_[3], false);
  body << "],\"chunks\":[";
  constexpr const char* kNames[] = {"input_copy", "setup_h2d", "projections",
                                    "attn_hc",    "attention", "mlp_hc",
                                    "moe",        "mixer",     "head"};
  int covered = 0;
  for (size_t i = 0; i < chunk_count_; ++i) {
    auto& chunk = chunks_[i];
    if (i) body << ',';
    if (chunk.base != covered || chunk.rows <= 0 || chunk.rows > chunk_size_ ||
        chunk.rows > rows_ - covered)
      Fail("chunk_coverage");
    covered += chunk.rows;
    if (chunk.compute_logits != (i + 1 == chunks_.size()))
      Fail("head_coverage");
    if (chunk.tail_skipped &&
        (chunk.compute_logits || i + 1 == chunks_.size()))
      Fail("tail_skip_scope");
    const auto total = measure(chunk.events.front(), chunk.events.back());
    body << "{\"base\":" << chunk.base << ",\"rows\":" << chunk.rows
         << ",\"compute_logits\":" << chunk.compute_logits
         << ",\"tail_skipped\":" << chunk.tail_skipped
         << ",\"logits_rows\":\"" << (chunk.last_row ? "last" : "all")
         << "\",\"stream_total_ms\":";
    if (total.ready)
      body << total.elapsed;
    else
      body << "null";
    body << ",\"stages\":[";
    double skipped_gap_ms = 0;
    bool skipped_gap_ready = true;
    for (size_t j = 0; j + 1 < kPoints; ++j) {
      if (j) body << ',';
      const bool skipped =
          (chunk.tail_skipped &&
           j >= static_cast<size_t>(InitGpuPoint::kMlpHcBegin)) ||
          (j == kPoints - 2 && !chunk.compute_logits);
      const auto result = span(kNames[j], chunk.events[j], chunk.events[j + 1],
                               skipped);
      if (skipped) {
        // Keep physical marker gaps even when no module was submitted. Each
        // elapsed value is binary32; accumulating at most four in binary64 is
        // covered by the parser's predeclared timing-arithmetic budget.
        skipped_gap_ready = skipped_gap_ready && result.ready;
        if (result.ready) skipped_gap_ms += result.elapsed;
      }
    }
    body << "],\"skipped_marker_gap_ms\":";
    if (skipped_gap_ready)
      body << skipped_gap_ms;
    else
      body << "null";
    body << '}';
  }
  body << ']';
  const bool complete = init_completed_ && first_content_written_ &&
                        chunk_count_ == chunks_.size() && covered == rows_;
  if (!complete) Fail("incomplete");
  // Event destruction is asynchronous; do not introduce a completion wait.
  auto destroy = [&](Event& event) {
    if (event.handle) {
      if (cudaEventDestroy(event.handle) != cudaSuccess) Fail("event_destroy");
      event.handle = nullptr;
    }
  };
  for (auto& event : outer_) destroy(event);
  for (auto& chunk : chunks_)
    for (auto& event : chunk.events) destroy(event);
  std::ostringstream output;
  output << std::setprecision(17) << std::boolalpha
         << "{\"schema_version\":1,\"response_id\":";
  Quote(output, response_id_.c_str());
  output << ",\"prompt_tokens\":" << rows_ << ",\"chunk_size\":" << chunk_size_
         << ",\"max_seq\":1,\"text_only\":true" << ",\"complete\":" << complete
         << ",\"valid\":" << !error_ << ",\"error\":";
  Quote(output, error_ ? error_ : "");
  output << ",\"diagnostic_only\":true,\"host_origin\":\"handle_chat_entry\""
         << ",\"unit\":\"ms\",\"timing_setup_host_ms\":" << setup_host_ms_
         << ",\"cuda_record_host_ms\":" << record_host_ms_
         << ",\"trunk_bytes\":" << trunk_bytes_
         << ",\"stream_time_is_gpu_active\":false"
         << ",\"host_and_stream_times_additive\":false" << body.str();
  // The final field serialization and fprintf itself are outside this sample.
  const double report_host_ms =
      std::chrono::duration<double, std::milli>(Clock::now() - report_begin)
          .count();
  output << ",\"report_host_ms\":" << report_host_ms << '}';
  std::fprintf(stderr, "[q4t][mtp_init_timing] %s\n", output.str().c_str());
}

}  // namespace q4t::mtp
