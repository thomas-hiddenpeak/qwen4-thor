#include "q4t/trace/mtp_verify_moe_timing.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <iomanip>
#include <limits>
#include <sstream>
#include <utility>
#include <vector>

namespace q4t::trace {
namespace {
using Clock = std::chrono::steady_clock;
constexpr std::array kSteps = {0, 1, 32, 63};
constexpr std::array kLayers = {0, 15, 32, 47};
constexpr std::array kOrdinals = {0, 5, 10, 15};
constexpr size_t kEventsPerCall = 31;
constexpr size_t kEventCapacity = 496;
constexpr const char* kStepNames[] = {"engine_begin",        "draft_end",
                                      "verify_readback_end", "accept_end",
                                      "extend_readback_end", "engine_end"};
constexpr const char* kHostNames[] = {
    "moe_begin",       "routed_begin", "counts_wait_begin",
    "counts_wait_end", "offsets_end",  "expert_loop_begin",
    "expert_loop_end", "routed_end",   "moe_end"};
constexpr const char* kGpuNames[] = {
    "moe_begin",       "router_end",     "topk_zero_end", "list_end",
    "counts_copy_end", "experts_joined", "routed_end",    "shared_gu_end",
    "shared_act_end",  "shared_dn_end",  "moe_end"};
constexpr const char* kGpuIntervals[] = {
    "router",          "topk_zero",     "token_list", "counts_copy",
    "parallel_window", "routed_finish", "shared_gu",  "shared_activation",
    "shared_dn",       "combine"};
constexpr const char* kExpertNames[] = {"gather_begin", "gather_end", "gu_end",
                                        "activation_end", "dn_end"};
constexpr const char* kExpertIntervals[] = {"gather_quant", "gu",
                                            "swiglu_quant", "dn"};
thread_local MtpVerifyMoeStep* active_step = nullptr;

class CudaEvents final : public MtpVerifyMoeEvents {
 public:
  cudaError_t Create(void** handle) noexcept override {
    cudaEvent_t event = nullptr;
    const auto result = cudaEventCreate(&event);
    *handle = event;
    return result;
  }
  cudaError_t Record(void* handle, cudaStream_t stream) noexcept override {
    return cudaEventRecord(static_cast<cudaEvent_t>(handle), stream);
  }
  cudaError_t Query(void* handle) noexcept override {
    return cudaEventQuery(static_cast<cudaEvent_t>(handle));
  }
  cudaError_t Elapsed(float* ms, void* begin, void* end) noexcept override {
    return cudaEventElapsedTime(ms, static_cast<cudaEvent_t>(begin),
                                static_cast<cudaEvent_t>(end));
  }
  cudaError_t Destroy(void* handle) noexcept override {
    return cudaEventDestroy(static_cast<cudaEvent_t>(handle));
  }
};
CudaEvents default_events;

void Quote(std::ostream& out, std::string_view text) {
  out << '"';
  for (unsigned char c : text) {
    if (c == '"' || c == '\\') {
      out << '\\' << c;
    } else if (c < 32) {
      constexpr char kHex[] = "0123456789abcdef";
      out << "\\u00" << kHex[c >> 4] << kHex[c & 15];
    } else {
      out << c;
    }
  }
  out << '"';
}
void Number(std::ostream& out, double value) {
  if (std::isfinite(value))
    out << value;
  else
    out << "null";
}
template <size_t N>
int Find(const std::array<int, N>& values, int value) noexcept {
  const auto it = std::find(values.begin(), values.end(), value);
  return it == values.end() ? -1 : static_cast<int>(it - values.begin());
}
double FloatUlp(double value) noexcept {
  const float f = static_cast<float>(value);
  const float up = std::nextafter(f, std::numeric_limits<float>::infinity());
  return std::isfinite(up) ? static_cast<double>(up) - f
                           : static_cast<double>(f) - std::nextafter(f, 0.0f);
}
template <size_t N>
bool Closed(const std::array<double, N>& values, double total) noexcept {
  if (!std::isfinite(total)) return false;
  double sum = 0, budget = FloatUlp(total);
  for (double value : values) {
    if (!std::isfinite(value)) return false;
    sum += value;
    budget += FloatUlp(value);
    budget +=
        std::nextafter(sum, std::numeric_limits<double>::infinity()) - sum;
  }
  return std::abs(sum - total) <= budget;
}
}  // namespace

struct MtpVerifyMoeTiming::Impl {
  struct Event {
    void* handle = nullptr;
    bool recorded = false;
    bool ready = false;
    int64_t at_ns = -1;
  };
  struct Expert {
    size_t marks = 0;
    std::array<double, 4> elapsed{};
    double total = std::numeric_limits<double>::quiet_NaN();
    Expert() { elapsed.fill(std::numeric_limits<double>::quiet_NaN()); }
  };
  struct Sample {
    bool used = false;
    int step = -1;
    int layer = -1;
    size_t host_count = 0;
    size_t gpu_count = 0;
    size_t count_size = 0;
    std::array<int64_t, 9> host_ns{};
    std::array<std::pair<int, int>, 40> counts{};
    std::array<cudaStream_t, 8> streams{};
    std::array<bool, 8> streams_seen{};
    std::array<double, 10> elapsed{};
    double total = std::numeric_limits<double>::quiet_NaN();
    std::array<Expert, 4> experts{};
    Sample() {
      host_ns.fill(-1);
      elapsed.fill(std::numeric_limits<double>::quiet_NaN());
    }
  };
  std::string response_id;
  int prompt_tokens;
  int max_tokens;
  int k;
  bool supported;
  Clock::time_point origin = Clock::now();
  MtpVerifyMoeEvents* backend;
  std::vector<MtpVerifyMoeStep> steps;
  size_t step_count = 0;
  size_t layer_count = 0;
  std::array<Sample, 16> samples{};
  std::array<Event, kEventCapacity> events{};
  size_t created = 0;
  size_t recorded = 0;
  size_t ready = 0;
  const char* error = nullptr;
  bool finished = false;
  bool collected = false;
  bool complete = false;
  bool success = false;
  int generated = -1;
  int mtp_steps = -1;
  bool plain_tail = false;
  std::string finish_reason;
  std::string fallback;
  int64_t setup_ns = 0;

  Impl(std::string id, int prompt, int maximum, int drafts, bool enabled,
       MtpVerifyMoeEvents* operations)
      : response_id(std::move(id)),
        prompt_tokens(prompt),
        max_tokens(maximum),
        k(drafts),
        supported(enabled),
        backend(operations ? operations : &default_events) {}
  ~Impl() { Release(); }
  void Fail(const char* reason) noexcept {
    if (!error) error = reason ? reason : "invalid_unspecified";
  }
  int64_t Now() const noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() -
                                                                origin)
        .count();
  }
  void Release() noexcept {
    for (auto& event : events) {
      if (!event.handle) continue;
      if (backend->Destroy(event.handle) != cudaSuccess)
        Fail("event_destroy_error");
      event.handle = nullptr;
    }
  }
  void BindStream(Sample& sample, int index, cudaStream_t stream) noexcept {
    if (index < 0 || index >= 8) {
      Fail("stream_index");
      return;
    }
    if (sample.streams_seen[index] && sample.streams[index] != stream)
      Fail("stream_changed");
    for (int i = 0; i < 8; ++i)
      if (i != index && sample.streams_seen[i] && sample.streams[i] == stream)
        Fail("stream_alias");
    sample.streams_seen[index] = true;
    sample.streams[index] = stream;
  }
  void Record(size_t index, cudaStream_t stream) noexcept {
    if (collected) {
      Fail("record_after_finish");
      return;
    }
    if (index >= events.size()) {
      Fail("event_capacity");
      return;
    }
    auto& event = events[index];
    if (event.recorded || !event.handle) {
      Fail(event.recorded ? "duplicate_event" : "missing_event_handle");
      return;
    }
    event.at_ns = Now();
    if (backend->Record(event.handle, stream) != cudaSuccess) {
      Fail("event_record_error");
      return;
    }
    event.recorded = true;
    ++recorded;
  }
  double Elapsed(size_t begin, size_t end) noexcept {
    const auto& a = events[begin];
    const auto& b = events[end];
    if (!a.recorded || !b.recorded || !a.ready || !b.ready) {
      Fail("missing_or_unready_event");
      return std::numeric_limits<double>::quiet_NaN();
    }
    float ms = 0;
    if (backend->Elapsed(&ms, a.handle, b.handle) != cudaSuccess) {
      Fail("event_elapsed_error");
      return std::numeric_limits<double>::quiet_NaN();
    }
    if (!std::isfinite(ms) || ms < 0) {
      Fail("event_elapsed_nonfinite_or_negative");
      return std::numeric_limits<double>::quiet_NaN();
    }
    return static_cast<double>(ms);
  }
  void Collect() noexcept {
    if (collected) return;
    collected = true;
    for (auto& event : events) {
      if (!event.recorded) continue;
      const auto result = backend->Query(event.handle);
      event.ready = result == cudaSuccess;
      if (event.ready)
        ++ready;
      else
        Fail(result == cudaErrorNotReady ? "event_not_ready"
                                         : "event_query_error");
    }
    for (size_t i = 0; i < samples.size(); ++i) {
      auto& sample = samples[i];
      if (!sample.used) continue;
      const size_t base = i * kEventsPerCall;
      for (size_t p = 0; p < sample.elapsed.size(); ++p)
        sample.elapsed[p] = Elapsed(base + p, base + p + 1);
      sample.total = Elapsed(base, base + 10);
      if (!Closed(sample.elapsed, sample.total))
        Fail("gpu_intervals_not_closed");
      for (size_t e = 0; e < kOrdinals.size(); ++e) {
        if (static_cast<size_t>(kOrdinals[e]) >= sample.count_size) continue;
        auto& expert = sample.experts[e];
        const size_t offset = base + 11 + e * 5;
        for (size_t p = 0; p < expert.elapsed.size(); ++p)
          expert.elapsed[p] = Elapsed(offset + p, offset + p + 1);
        expert.total = Elapsed(offset, offset + 4);
        if (!Closed(expert.elapsed, expert.total))
          Fail("expert_intervals_not_closed");
      }
    }
  }
};

MtpVerifyMoeTiming::MtpVerifyMoeTiming(std::string response_id,
                                       int prompt_tokens, int max_tokens, int k,
                                       bool supported,
                                       MtpVerifyMoeEvents* events)
    : impl_(std::make_unique<Impl>(std::move(response_id), prompt_tokens,
                                   max_tokens, k, supported, events)) {
  auto& p = *impl_;
  if (!supported || prompt_tokens <= 0 || max_tokens <= 0 || k != 3) {
    p.Fail("unsupported_request");
    p.supported = false;
    return;
  }
  p.steps.resize(static_cast<size_t>(std::min(max_tokens, 1024)));
  // Allocation that may throw is complete before the first event exists.
  for (auto& event : p.events) {
    const auto result = p.backend->Create(&event.handle);
    if (event.handle) ++p.created;
    if (result != cudaSuccess || !event.handle) {
      p.Fail("event_create_error");
      break;
    }
  }
  p.setup_ns = p.Now();
}

MtpVerifyMoeTiming::~MtpVerifyMoeTiming() noexcept {
  if (!impl_) return;
  if (!impl_->finished) impl_->Fail("request_incomplete");
  impl_->Collect();
  impl_->Release();
  try {
    Report();
  } catch (...) {
    // No prompt or token content is emitted, including allocation failures.
    std::fprintf(stderr,
                 "[q4t][mtp_verify_moe_timing] {\"schema_version\":1,"
                 "\"valid\":false,\"complete\":false,"
                 "\"error\":\"report_exception\"}\n");
  }
}

int64_t MtpVerifyMoeTiming::NowNs() const noexcept { return impl_->Now(); }
void MtpVerifyMoeTiming::Invalidate(const char* reason) noexcept {
  impl_->Fail(reason);
}

MtpVerifyMoeStep* MtpVerifyMoeTiming::BeginStep(int position, int k) noexcept {
  auto& p = *impl_;
  if (!p.supported) return nullptr;
  if (p.finished || k != 3 || position < p.prompt_tokens) {
    p.Fail("step_contract");
    return nullptr;
  }
  if (p.step_count >= p.steps.size()) {
    p.Fail("step_capacity");
    return nullptr;
  }
  auto& step = p.steps[p.step_count];
  step.owner_ = this;
  step.index_ = static_cast<int>(p.step_count++);
  step.position_ = position;
  step.host_ns_.fill(-1);
  return &step;
}

void MtpVerifyMoeStep::Invalidate(const char* reason) noexcept {
  if (owner_) owner_->Invalidate(reason);
}
void MtpVerifyMoeStep::Mark(VerifyMoeStepPoint point) noexcept {
  if (!owner_) return;
  const auto index = static_cast<size_t>(point);
  if (index >= host_ns_.size() || index != marks_) {
    Invalidate("step_marker_order");
    return;
  }
  host_ns_[marks_++] = owner_->NowNs();
}
void MtpVerifyMoeStep::SetEngineResult(int accepted_drafts, int returned,
                                       int verify_rows,
                                       int extend_rows) noexcept {
  if (!owner_) return;
  if (accepted_ >= 0 || accepted_drafts < 0 || accepted_drafts > 3 ||
      returned != accepted_drafts + 1 || verify_rows != 4 ||
      extend_rows != returned)
    Invalidate("engine_result_contract");
  accepted_ = accepted_drafts;
  returned_ = returned;
  verify_rows_ = verify_rows;
  extend_rows_ = extend_rows;
}
void MtpVerifyMoeStep::SetDelivery(int generated) noexcept {
  if (!owner_) return;
  if (delivered_ >= 0 || generated < 0 || generated > returned_)
    Invalidate("delivery_contract");
  delivered_ = generated;
}
MtpVerifyMoeCall* MtpVerifyMoeStep::BeginLayer(int layer, int rows) noexcept {
  if (!owner_) return nullptr;
  if (layer < 0 || layer >= 48 || rows != 4) {
    Invalidate("layer_shape");
    return nullptr;
  }
  if (static_cast<size_t>(layer) != layers_ || calls_[layer].seen_) {
    Invalidate("layer_order_or_duplicate");
    return nullptr;
  }
  ++layers_;
  auto& p = *owner_->impl_;
  ++p.layer_count;
  auto& call = calls_[layer];
  call.owner_ = owner_;
  call.step_ = index_;
  call.layer_ = layer;
  call.seen_ = true;
  const int si = Find(kSteps, index_), li = Find(kLayers, layer);
  if (si >= 0 && li >= 0) {
    call.sample_ = si * 4 + li;
    auto& sample = p.samples[call.sample_];
    sample.used = true;
    sample.step = index_;
    sample.layer = layer;
  }
  return &call;
}

void MtpVerifyMoeCall::MarkHost(VerifyMoeHostPoint point) noexcept {
  if (!owner_ || sample_ < 0) return;
  auto& p = *owner_->impl_;
  auto& sample = p.samples[sample_];
  const auto index = static_cast<size_t>(point);
  if (index >= sample.host_ns.size() || index != sample.host_count) {
    p.Fail("call_host_marker_order");
    return;
  }
  sample.host_ns[sample.host_count++] = p.Now();
}
void MtpVerifyMoeCall::MarkGpu(VerifyMoeGpuPoint point,
                               cudaStream_t stream) noexcept {
  if (!owner_ || sample_ < 0) return;
  auto& p = *owner_->impl_;
  auto& sample = p.samples[sample_];
  const auto index = static_cast<size_t>(point);
  if (index >= 11 || index != sample.gpu_count) {
    p.Fail("call_gpu_marker_order");
    return;
  }
  ++sample.gpu_count;
  p.BindStream(sample, 0, stream);
  p.Record(static_cast<size_t>(sample_) * kEventsPerCall + index, stream);
}
void MtpVerifyMoeCall::SetCounts(const int32_t* counts, int experts, int rows,
                                 int k) noexcept {
  if (!owner_) return;
  auto& p = *owner_->impl_;
  if (counts_set_) {
    p.Fail("duplicate_counts");
    return;
  }
  counts_set_ = true;
  if (!counts || experts != 512 || rows != 4 || k != 10) {
    p.Fail("counts_shape");
    ++invalid_counts_;
    return;
  }
  for (int e = 0; e < experts; ++e) {
    const int count = counts[e];
    if (count < 0 || count > 4) {
      ++invalid_counts_;
      continue;
    }
    ++histogram_[count];
    routes_ += count;
    if (count == 0) continue;
    ++active_;
    if (sample_ >= 0) {
      auto& sample = p.samples[sample_];
      if (sample.count_size == sample.counts.size()) {
        p.Fail("sparse_counts_capacity");
      } else {
        sample.counts[sample.count_size++] = {e, count};
      }
    }
  }
  if (invalid_counts_ != 0 || routes_ != 40 || active_ < 10 || active_ > 40)
    p.Fail("counts_contract");
}
void MtpVerifyMoeCall::SetStreams(int streams) noexcept {
  if (!owner_) return;
  if (streams_set_ || streams < 1 || streams > 8)
    owner_->Invalidate("streams_contract");
  streams_set_ = true;
  streams_ = streams;
}
void MtpVerifyMoeCall::MarkExpert(int ordinal, VerifyMoeExpertPoint point,
                                  cudaStream_t stream) noexcept {
  if (!owner_ || sample_ < 0) return;
  const int selected = Find(kOrdinals, ordinal);
  if (selected < 0) return;
  auto& p = *owner_->impl_;
  auto& sample = p.samples[sample_];
  if (!counts_set_ || !streams_set_ || streams_ < 1 || streams_ > 8 ||
      ordinal >= static_cast<int>(sample.count_size)) {
    p.Fail("expert_metadata");
    return;
  }
  auto& expert = sample.experts[selected];
  const auto index = static_cast<size_t>(point);
  if (index >= 5 || index != expert.marks) {
    p.Fail("expert_marker_order");
    return;
  }
  ++expert.marks;
  p.BindStream(sample, ordinal % streams_, stream);
  p.Record(static_cast<size_t>(sample_) * kEventsPerCall + 11 +
               static_cast<size_t>(selected) * 5 + index,
           stream);
}

MtpVerifyMoeStep* ActiveMtpVerifyMoeStep() noexcept { return active_step; }
MtpVerifyMoeScope::MtpVerifyMoeScope(MtpVerifyMoeStep* step) noexcept
    : previous_(active_step) {
  active_step = step;
}
MtpVerifyMoeScope::~MtpVerifyMoeScope() noexcept { active_step = previous_; }

void MtpVerifyMoeTiming::Finish(int generated, int mtp_steps, bool plain_tail,
                                std::string_view finish_reason,
                                std::string_view fallback,
                                bool success) noexcept {
  auto& p = *impl_;
  if (p.finished) {
    p.Fail("duplicate_finish");
    return;
  }
  p.finished = true;
  p.generated = generated;
  p.mtp_steps = mtp_steps;
  p.plain_tail = plain_tail;
  p.success = success;
  try {
    p.finish_reason.assign(finish_reason);
    p.fallback.assign(fallback);
  } catch (...) {
    p.Fail("finish_allocation");
  }
  if (!success || plain_tail || fallback != "none" ||
      (finish_reason != "length" && finish_reason != "stop") || generated < 0 ||
      generated > p.max_tokens || mtp_steps != static_cast<int>(p.step_count) ||
      p.step_count == 0)
    p.Fail("request_result_contract");
  int64_t delivered = 0;
  int64_t next_position = p.prompt_tokens;
  for (size_t i = 0; i < p.step_count; ++i) {
    const auto& step = p.steps[i];
    if (step.marks_ != 6 || step.layers_ != 48 || step.accepted_ < 0 ||
        step.delivered_ < 0 || step.position_ != next_position ||
        (i + 1 < p.step_count && step.delivered_ != step.returned_))
      p.Fail("step_incomplete");
    for (size_t j = 1; j < step.marks_; ++j)
      if (step.host_ns_[j] < step.host_ns_[j - 1])
        p.Fail("step_host_time_order");
    next_position += step.returned_;
    delivered += step.delivered_;
    for (const auto& call : step.calls_)
      if (!call.seen_ || !call.counts_set_ || !call.streams_set_)
        p.Fail("layer_incomplete");
  }
  if (delivered != generated) p.Fail("generated_count_mismatch");
  for (const auto& sample : p.samples) {
    if (!sample.used) continue;
    if (sample.host_count != 9 || sample.gpu_count != 11)
      p.Fail("sample_incomplete");
    const auto& step = p.steps[sample.step];
    for (size_t j = 1; j < sample.host_count; ++j)
      if (sample.host_ns[j] < sample.host_ns[j - 1])
        p.Fail("call_host_time_order");
    if (sample.host_ns[0] < step.host_ns_[1] ||
        sample.host_ns[8] > step.host_ns_[2])
      p.Fail("call_outside_verify");
    const size_t base =
        static_cast<size_t>(&sample - p.samples.data()) * kEventsPerCall;
    for (size_t j = 0; j < 11; ++j)
      if (p.events[base + j].at_ns < sample.host_ns[0] ||
          p.events[base + j].at_ns > sample.host_ns[8])
        p.Fail("gpu_marker_outside_call");
    for (size_t e = 0; e < kOrdinals.size(); ++e) {
      const bool present =
          static_cast<size_t>(kOrdinals[e]) < sample.count_size;
      if (sample.experts[e].marks != (present ? 5u : 0u))
        p.Fail("expert_incomplete");
      if (present)
        for (size_t j = 0; j < 5; ++j) {
          const auto ns = p.events[base + 11 + e * 5 + j].at_ns;
          if (ns < sample.host_ns[5] || ns > sample.host_ns[6])
            p.Fail("expert_marker_outside_loop");
        }
    }
  }
  p.Collect();
  p.complete = !p.error;
}

void MtpVerifyMoeTiming::Report() {
  auto& p = *impl_;
  const int64_t begin = p.Now();
  std::ostringstream out;
  out << std::setprecision(17) << std::boolalpha;
  out << "{\"schema_version\":1,\"policy\":\"t4_moe_16_calls_v1\","
         "\"response_id\":";
  Quote(out, p.response_id);
  out << ",\"diagnostic_only\":true,\"valid\":" << (!p.error && p.complete)
      << ",\"complete\":" << (!p.error && p.complete) << ",\"error\":";
  Quote(out, p.error ? p.error : "");
  out << ",\"supported\":" << p.supported
      << ",\"prompt_tokens\":" << p.prompt_tokens
      << ",\"max_tokens\":" << p.max_tokens << ",\"k\":" << p.k
      << ",\"max_seq\":1,\"text_only\":true,\"known_positions\":true,"
         "\"host_unit\":\"ns\",\"gpu_unit\":\"ms\","
         "\"stream_time_is_gpu_active\":false,"
         "\"host_and_stream_times_additive\":false,\"setup_host_ns\":"
      << p.setup_ns << ",\"step_capacity\":" << p.steps.size()
      << ",\"step_count\":" << p.step_count
      << ",\"layer_count\":" << p.layer_count << ",\"sample_call_count\":"
      << std::count_if(p.samples.begin(), p.samples.end(),
                       [](const auto& sample) { return sample.used; })
      << ",\"event_capacity\":496,\"event_created\":" << p.created
      << ",\"event_recorded\":" << p.recorded << ",\"event_ready\":" << p.ready
      << ",\"sample_steps\":[0,1,32,63],\"sample_layers\":[0,15,32,47],"
         "\"sample_ordinals\":[0,5,10,15],\"generated\":"
      << p.generated << ",\"mtp_steps\":" << p.mtp_steps
      << ",\"plain_tail\":" << p.plain_tail << ",\"finish_reason\":";
  Quote(out, p.finish_reason);
  out << ",\"fallback\":";
  Quote(out, p.fallback);
  out << ",\"success\":" << p.success << ",\"steps\":[";
  const auto host_marks = [&](const auto& values, size_t count,
                              const char* const* names) {
    out << '[';
    for (size_t i = 0; i < count; ++i) {
      if (i) out << ',';
      out << "{\"name\":";
      Quote(out, names[i]);
      out << ",\"at_ns\":" << values[i] << '}';
    }
    out << ']';
  };
  for (size_t i = 0; i < p.step_count; ++i) {
    if (i) out << ',';
    const auto& step = p.steps[i];
    out << "{\"index\":" << i << ",\"position\":" << step.position_
        << ",\"k\":3,\"host_marks\":";
    host_marks(step.host_ns_, step.marks_, kStepNames);
    out << ",\"accepted_drafts\":" << step.accepted_
        << ",\"returned\":" << step.returned_
        << ",\"verify_rows\":" << step.verify_rows_
        << ",\"extend_rows\":" << step.extend_rows_
        << ",\"delivered\":" << step.delivered_ << ",\"layers\":[";
    bool comma = false;
    for (const auto& call : step.calls_) {
      if (!call.seen_) continue;
      if (comma) out << ',';
      comma = true;
      out << "{\"layer\":" << call.layer_
          << ",\"rows\":4,\"experts\":512,\"top_k\":10,\"histogram\":[";
      for (size_t h = 0; h < call.histogram_.size(); ++h) {
        if (h) out << ',';
        out << call.histogram_[h];
      }
      out << "],\"routes\":" << call.routes_
          << ",\"active_experts\":" << call.active_
          << ",\"invalid_counts\":" << call.invalid_counts_
          << ",\"streams\":" << call.streams_
          << ",\"sampled\":" << (call.sample_ >= 0) << '}';
    }
    out << "]}";
  }
  out << "],\"samples\":[";
  const auto gpu_marks = [&](size_t base, size_t count,
                             const char* const* names) {
    out << '[';
    for (size_t i = 0; i < count; ++i) {
      if (i) out << ',';
      const auto& event = p.events[base + i];
      out << "{\"name\":";
      Quote(out, names[i]);
      out << ",\"at_ns\":" << event.at_ns << ",\"recorded\":" << event.recorded
          << ",\"ready\":" << event.ready << '}';
    }
    out << ']';
  };
  const auto intervals = [&](const auto& values, const char* const* names,
                             const char* const* points) {
    out << '[';
    for (size_t i = 0; i < values.size(); ++i) {
      if (i) out << ',';
      out << "{\"name\":";
      Quote(out, names[i]);
      out << ",\"begin\":";
      Quote(out, points[i]);
      out << ",\"end\":";
      Quote(out, points[i + 1]);
      out << ",\"stream_ms\":";
      Number(out, values[i]);
      out << '}';
    }
    out << ']';
  };
  bool comma = false;
  for (size_t i = 0; i < p.samples.size(); ++i) {
    const auto& sample = p.samples[i];
    if (!sample.used) continue;
    if (comma) out << ',';
    comma = true;
    const auto& call = p.steps[sample.step].calls_[sample.layer];
    const size_t base = i * kEventsPerCall;
    out << "{\"step\":" << sample.step << ",\"layer\":" << sample.layer
        << ",\"rows\":4,\"streams\":" << call.streams_ << ",\"counts\":[";
    for (size_t j = 0; j < sample.count_size; ++j) {
      if (j) out << ',';
      out << "{\"expert_id\":" << sample.counts[j].first
          << ",\"rows\":" << sample.counts[j].second << '}';
    }
    out << "],\"host_marks\":";
    host_marks(sample.host_ns, sample.host_count, kHostNames);
    out << ",\"gpu_marks\":";
    gpu_marks(base, 11, kGpuNames);
    out << ",\"gpu_intervals\":";
    intervals(sample.elapsed, kGpuIntervals, kGpuNames);
    out << ",\"stream_total_ms\":";
    Number(out, sample.total);
    out << ",\"experts\":[";
    for (size_t e = 0; e < kOrdinals.size(); ++e) {
      if (e) out << ',';
      const int ordinal = kOrdinals[e];
      const bool present = static_cast<size_t>(ordinal) < sample.count_size;
      out << "{\"ordinal\":" << ordinal << ",\"present\":" << present
          << ",\"expert_id\":" << (present ? sample.counts[ordinal].first : -1)
          << ",\"rows\":" << (present ? sample.counts[ordinal].second : 0)
          << ",\"stream_index\":"
          << (present && call.streams_ > 0 ? ordinal % call.streams_ : -1)
          << ",\"gpu_marks\":";
      if (present) {
        gpu_marks(base + 11 + e * 5, 5, kExpertNames);
        out << ",\"gpu_intervals\":";
        intervals(sample.experts[e].elapsed, kExpertIntervals, kExpertNames);
      } else {
        out << "[],\"gpu_intervals\":[]";
      }
      out << ",\"stream_total_ms\":";
      Number(out, sample.experts[e].total);
      out << '}';
    }
    out << "]}";
  }
  // This measures only the JSON construction above, excluding Collect,
  // Release, out.str() and fprintf. Finish/report follow the response's DONE;
  // their costs can overlap a later request and are not this client's latency.
  out << "],\"report_host_ns\":" << p.Now() - begin << '}';
  const auto line = out.str();
  std::fprintf(stderr, "[q4t][mtp_verify_moe_timing] %s\n", line.c_str());
}

}  // namespace q4t::trace
