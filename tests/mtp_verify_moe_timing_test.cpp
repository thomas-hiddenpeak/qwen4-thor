// Standalone host contracts. Link collector + src/io/json.cpp + cudart;
// every collector receives FakeEvents, so these tests never initialize CUDA.
#include "q4t/io/json.h"
#include "q4t/trace/mtp_verify_moe_timing.h"

#include <unistd.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <functional>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

namespace {
using q4t::io::Json;
using q4t::trace::ActiveMtpVerifyMoeStep;
using q4t::trace::MtpVerifyMoeCall;
using q4t::trace::MtpVerifyMoeEvents;
using q4t::trace::MtpVerifyMoeScope;
using q4t::trace::MtpVerifyMoeStep;
using q4t::trace::MtpVerifyMoeTiming;
using q4t::trace::VerifyMoeExpertPoint;
using q4t::trace::VerifyMoeGpuPoint;
using q4t::trace::VerifyMoeHostPoint;
using q4t::trace::VerifyMoeStepPoint;
constexpr std::string_view kPrefix = "[q4t][mtp_verify_moe_timing] ";

void Require(bool value, std::string_view message) {
  if (!value) throw std::runtime_error(std::string(message));
}
class FakeEvents final : public MtpVerifyMoeEvents {
 public:
  struct Event {
    int index = -1;
    int tick = -1;
    bool destroyed = false;
  };
  std::array<Event, 496> events{};
  int created = 0, records = 0, queries = 0, elapsed = 0, destroyed = 0;
  int fail_create = -1, fail_record = -1, fail_query = -1;
  int not_ready = -1, fail_elapsed = -1, fail_destroy = -1;
  bool nonfinite = false, negative = false, open_interval = false;
  cudaError_t Create(void** handle) noexcept override {
    *handle = nullptr;
    if (created == fail_create) return cudaErrorMemoryAllocation;
    if (created >= 496) return cudaErrorInvalidValue;
    auto& event = events[created];
    event.index = created++;
    *handle = &event;
    return cudaSuccess;
  }
  cudaError_t Record(void* handle, cudaStream_t) noexcept override {
    auto& event = *static_cast<Event*>(handle);
    const int call = records++;
    if (call == fail_record) return cudaErrorInvalidValue;
    event.tick = call;
    return cudaSuccess;
  }
  cudaError_t Query(void* handle) noexcept override {
    const auto& event = *static_cast<Event*>(handle);
    ++queries;
    if (event.index == fail_query) return cudaErrorInvalidValue;
    if (event.index == not_ready) return cudaErrorNotReady;
    return event.tick >= 0 ? cudaSuccess : cudaErrorInvalidValue;
  }
  cudaError_t Elapsed(float* ms, void* begin, void* end) noexcept override {
    const auto& a = *static_cast<Event*>(begin);
    const auto& b = *static_cast<Event*>(end);
    if (elapsed++ == fail_elapsed) return cudaErrorInvalidValue;
    *ms = static_cast<float>(b.tick - a.tick) / 10.0f;
    if (nonfinite) *ms = std::numeric_limits<float>::quiet_NaN();
    if (negative) *ms = -1;
    if (open_interval && b.index - a.index == 10) *ms += 1.0f;
    return cudaSuccess;
  }
  cudaError_t Destroy(void* handle) noexcept override {
    auto& event = *static_cast<Event*>(handle);
    if (event.destroyed) return cudaErrorInvalidValue;
    event.destroyed = true;
    ++destroyed;
    return event.index == fail_destroy ? cudaErrorInvalidValue : cudaSuccess;
  }
};

// A regular anonymous file avoids pipe-capacity deadlocks for the complete
// 64-step fixture. All descriptors are restored even when an assertion throws.
class Capture {
 public:
  Capture() {
    file_ = std::tmpfile();
    Require(file_ != nullptr, "tmpfile");
    std::fflush(stderr);
    saved_ = dup(STDERR_FILENO);
    if (saved_ < 0 || dup2(fileno(file_), STDERR_FILENO) < 0) {
      if (saved_ >= 0) close(saved_);
      std::fclose(file_);
      file_ = nullptr;
      throw std::runtime_error("stderr capture");
    }
  }
  ~Capture() {
    Restore();
    if (file_) std::fclose(file_);
  }
  std::string Finish() {
    Restore();
    Require(!restore_failed_, "restore stderr");
    Require(std::fseek(file_, 0, SEEK_SET) == 0, "rewind capture");
    std::string text;
    std::array<char, 8192> buffer{};
    for (;;) {
      const size_t n = std::fread(buffer.data(), 1, buffer.size(), file_);
      text.append(buffer.data(), n);
      if (n < buffer.size()) break;
    }
    Require(!std::ferror(file_), "read capture");
    return text;
  }

 private:
  void Restore() noexcept {
    if (saved_ < 0) return;
    std::fflush(stderr);
    restore_failed_ = dup2(saved_, STDERR_FILENO) < 0;
    close(saved_);
    saved_ = -1;
  }
  FILE* file_ = nullptr;
  int saved_ = -1;
  bool restore_failed_ = false;
};
const Json& Field(const Json& value, std::string_view name) {
  const Json* found = value.Find(std::string(name));
  Require(found != nullptr, "missing field");
  return *found;
}
const std::vector<Json>& Array(const Json& value, std::string_view name) {
  const auto& found = Field(value, name);
  Require(found.IsArray(), "array field");
  return found.array;
}
void Valid(const Json& record) {
  Require(record.GetBool("valid") && record.GetBool("complete") &&
              record.GetString("error") == "",
          "expected valid complete record");
}
void Invalid(const Json& record, std::string_view reason = {}) {
  Require(!record.GetBool("valid") && !record.GetBool("complete") &&
              !record.GetString("error").empty(),
          "expected invalid record");
  if (!reason.empty()) Require(record.GetString("error") == reason, reason);
}
struct Result {
  Json json;
  std::string raw;
};
Result Scenario(FakeEvents& events,
                const std::function<void(MtpVerifyMoeTiming&)>& action,
                int max_tokens = 4, int k = 3, bool supported = true,
                std::string id = "finite-host") {
  Capture capture;
  {
    MtpVerifyMoeTiming timing(std::move(id), 1024, max_tokens, k, supported,
                              &events);
    action(timing);
  }
  Result result;
  result.raw = capture.Finish();
  Require(result.raw.starts_with(kPrefix) && result.raw.ends_with('\n') &&
              result.raw.find('\n') == result.raw.size() - 1,
          "exactly one complete log record");
  q4t::io::JsonParseLimits limits;
  limits.reject_duplicate_keys = true;
  const auto status = q4t::io::ParseJson(result.raw.substr(kPrefix.size()),
                                         &result.json, false, limits);
  Require(status.ok() && result.json.IsObject(), "valid JSON");
  Require(events.created == events.destroyed, "every event released once");
  return result;
}
cudaStream_t Stream(int index) {
  static std::array<int, 8> tags{};
  return index == 0 ? nullptr : reinterpret_cast<cudaStream_t>(&tags[index]);
}
struct Options {
  int omit_step = -1, omit_host = -1, omit_gpu = -1, omit_expert = -1;
  int omit_layer = -1;
  bool duplicate_layer = false, duplicate_counts = false;
  bool missing_counts = false, missing_streams = false;
  bool bad_counts = false, wrong_stream = false, absent_ordinal = false;
  int rows = 4, counts_k = 10, streams = 4;
  int distribution = 0;
  int accepted = 3, delivered = 4;
};
void Layer(MtpVerifyMoeCall& call, const Options& options) {
  const auto host = [&](VerifyMoeHostPoint point) {
    if (static_cast<int>(point) != options.omit_host) call.MarkHost(point);
  };
  const auto gpu = [&](VerifyMoeGpuPoint point) {
    if (static_cast<int>(point) != options.omit_gpu)
      call.MarkGpu(point, nullptr);
  };
  host(VerifyMoeHostPoint::kMoeBegin);
  gpu(VerifyMoeGpuPoint::kMoeBegin);
  gpu(VerifyMoeGpuPoint::kRouterEnd);
  gpu(VerifyMoeGpuPoint::kTopkZeroEnd);
  host(VerifyMoeHostPoint::kRoutedBegin);
  gpu(VerifyMoeGpuPoint::kListEnd);
  host(VerifyMoeHostPoint::kCountsWaitBegin);
  gpu(VerifyMoeGpuPoint::kCountsCopyEnd);
  host(VerifyMoeHostPoint::kCountsWaitEnd);
  std::array<int32_t, 512> counts{};
  int active = 40;
  if (options.distribution == 1) {
    active = 10;
    std::fill_n(counts.begin(), active, 4);
  } else if (options.distribution == 2) {
    active = 16;
    for (int e = 0; e < active; ++e) counts[e] = e / 4 + 1;
  } else {
    std::fill_n(counts.begin(), active, 1);
  }
  if (options.bad_counts) counts[0] = 5;
  if (!options.missing_counts) {
    call.SetCounts(counts.data(), 512, 4, options.counts_k);
    if (options.duplicate_counts)
      call.SetCounts(counts.data(), 512, 4, options.counts_k);
  }
  host(VerifyMoeHostPoint::kOffsetsEnd);
  if (!options.missing_streams) call.SetStreams(options.streams);
  host(VerifyMoeHostPoint::kExpertLoopBegin);
  // Exercise every active ordinal: nonselected experts must not record.
  for (int ordinal = 0; ordinal < active; ++ordinal)
    for (int point = 0; point < 5; ++point) {
      if (point == options.omit_expert) continue;
      const int stream = options.wrong_stream && ordinal == 0 ? 1 : ordinal % 4;
      call.MarkExpert(ordinal, static_cast<VerifyMoeExpertPoint>(point),
                      Stream(stream));
    }
  if (options.absent_ordinal)
    call.MarkExpert(15, VerifyMoeExpertPoint::kGatherBegin, Stream(3));
  host(VerifyMoeHostPoint::kExpertLoopEnd);
  gpu(VerifyMoeGpuPoint::kExpertsJoined);
  gpu(VerifyMoeGpuPoint::kRoutedEnd);
  host(VerifyMoeHostPoint::kRoutedEnd);
  gpu(VerifyMoeGpuPoint::kSharedGuEnd);
  gpu(VerifyMoeGpuPoint::kSharedActEnd);
  gpu(VerifyMoeGpuPoint::kSharedDnEnd);
  gpu(VerifyMoeGpuPoint::kMoeEnd);
  host(VerifyMoeHostPoint::kMoeEnd);
}
void Step(MtpVerifyMoeTiming& timing, int index = 0,
          const Options& options = {}) {
  auto* step = timing.BeginStep(1024 + index * 4, 3);
  Require(step != nullptr, "expected step");
  const auto mark = [&](VerifyMoeStepPoint point) {
    if (static_cast<int>(point) != options.omit_step) step->Mark(point);
  };
  mark(VerifyMoeStepPoint::kEngineBegin);
  mark(VerifyMoeStepPoint::kDraftEnd);
  {
    MtpVerifyMoeScope scope(step);
    for (int layer = 0; layer < 48; ++layer) {
      if (layer == options.omit_layer) continue;
      auto* call = ActiveMtpVerifyMoeStep()->BeginLayer(layer, options.rows);
      if (call) Layer(*call, options);
      if (layer == 0 && options.duplicate_layer) step->BeginLayer(0, 4);
    }
  }
  mark(VerifyMoeStepPoint::kVerifyReadbackEnd);
  mark(VerifyMoeStepPoint::kAcceptEnd);
  mark(VerifyMoeStepPoint::kExtendReadbackEnd);
  mark(VerifyMoeStepPoint::kEngineEnd);
  step->SetEngineResult(options.accepted, 4, 4, 4);
  step->SetDelivery(options.delivered);
}
Result One(FakeEvents& events, const Options& options = {}, int generated = 4) {
  return Scenario(events, [&](auto& timing) {
    Step(timing, 0, options);
    timing.Finish(generated, 1, false, "length", "none", true);
  });
}
}  // namespace

int main(int argc, char** argv) {
  std::string fixture_path;
  if (argc == 3 && std::string_view(argv[1]) == "--fixture")
    fixture_path = argv[2];
  else if (argc != 1)
    return 2;
  int cases = 0;
  const auto run = [&](const char* name, const std::function<void()>& test) {
    test();
    ++cases;
    std::printf("MTP_VERIFY_MOE_HOST_CASE name=%s passed=1\n", name);
  };
  try {
    run("short_normal", [&] {
      FakeEvents events;
      const auto result = One(events);
      Valid(result.json);
      Require(events.created == 496 && events.records == 124 &&
                  events.queries == 124 && events.destroyed == 496,
              "short bounded event counts");
      Require(result.json.GetInt("layer_count") == 48 &&
                  result.json.GetInt("sample_call_count") == 4,
              "short census/sample sizes");
      const auto& leaf =
          Array(Array(result.json, "samples")[0], "gpu_intervals")[0];
      Require(Field(leaf, "stream_ms").number == static_cast<double>(0.1f),
              "binary32 elapsed round trip");
    });
    run("full_64_steps", [&] {
      FakeEvents events;
      const auto result = Scenario(
          events,
          [&](auto& timing) {
            for (int i = 0; i < 64; ++i) Step(timing, i);
            timing.Finish(256, 64, false, "length", "none", true);
          },
          256);
      Valid(result.json);
      Require(events.records == 496 && events.queries == 496 &&
                  result.json.GetInt("layer_count") == 3072 &&
                  result.json.GetInt("sample_call_count") == 16,
              "complete fixed sampling/census");
      if (!fixture_path.empty()) {
        FILE* file = std::fopen(fixture_path.c_str(), "wx");
        Require(file != nullptr, "fresh fixture output");
        const size_t written =
            std::fwrite(result.raw.data(), 1, result.raw.size(), file);
        const int closed = std::fclose(file);
        Require(written == result.raw.size() && closed == 0, "write fixture");
      }
    });
    run("absent_ordinals", [&] {
      FakeEvents events;
      Options options;
      options.distribution = 1;
      const auto result = One(events, options);
      Valid(result.json);
      Require(events.records == 84, "absent event count");
      const auto& experts = Array(Array(result.json, "samples")[0], "experts");
      Require(!experts[2].GetBool("present") &&
                  Field(experts[2], "stream_total_ms").IsNull() &&
                  Array(experts[2], "gpu_marks").empty(),
              "missing ordinals stay absent");
    });
    run("mixed_counts", [&] {
      FakeEvents events;
      Options options;
      options.distribution = 2;
      const auto result = One(events, options);
      Valid(result.json);
      const auto& histogram = Array(
          Array(Array(result.json, "steps")[0], "layers")[0], "histogram");
      Require(histogram[0].AsInt() == 496, "mixed zero counts");
      for (int i = 1; i < 5; ++i)
        Require(histogram[i].AsInt() == 4, "mixed nonzero counts");
    });
    run("tls_nested_and_threads", [&] {
      FakeEvents events;
      const auto result = Scenario(events, [&](auto& timing) {
        auto* step = timing.BeginStep(1024, 3);
        Require(ActiveMtpVerifyMoeStep() == nullptr, "TLS initially off");
        {
          MtpVerifyMoeScope outer(step);
          {
            MtpVerifyMoeScope inner(nullptr);
            Require(ActiveMtpVerifyMoeStep() == nullptr, "null scope");
          }
          Require(ActiveMtpVerifyMoeStep() == step, "nested restore");
          bool isolated = false;
          std::thread child([&] {
            isolated = ActiveMtpVerifyMoeStep() == nullptr;
            MtpVerifyMoeScope local(step);
            isolated = isolated && ActiveMtpVerifyMoeStep() == step;
          });
          child.join();
          Require(isolated && ActiveMtpVerifyMoeStep() == step,
                  "thread-local isolation");
        }
        Require(ActiveMtpVerifyMoeStep() == nullptr && events.records == 0,
                "inactive scope does no event work");
      });
      Invalid(result.json, "request_incomplete");
    });
    run("partial_create_cleanup", [&] {
      FakeEvents events;
      events.fail_create = 17;
      Invalid(One(events).json, "event_create_error");
      Require(events.created == 17 && events.destroyed == 17,
              "partial construction cleanup");
    });
    run("event_record_error", [&] {
      FakeEvents events;
      events.fail_record = 0;
      Invalid(One(events).json, "event_record_error");
    });
    run("event_query_error", [&] {
      FakeEvents events;
      events.fail_query = 0;
      Invalid(One(events).json, "event_query_error");
    });
    run("event_not_ready", [&] {
      FakeEvents events;
      events.not_ready = 0;
      Invalid(One(events).json, "event_not_ready");
    });
    run("event_elapsed_error", [&] {
      FakeEvents events;
      events.fail_elapsed = 0;
      Invalid(One(events).json, "event_elapsed_error");
    });
    run("event_nonfinite_or_negative", [&] {
      for (bool negative : {false, true}) {
        FakeEvents events;
        events.nonfinite = !negative;
        events.negative = negative;
        Invalid(One(events).json, "event_elapsed_nonfinite_or_negative");
      }
    });
    run("independent_gpu_closure", [&] {
      FakeEvents events;
      events.open_interval = true;
      Invalid(One(events).json, "gpu_intervals_not_closed");
    });
    run("destroy_error_is_reported", [&] {
      FakeEvents events;
      events.fail_destroy = 0;
      Invalid(One(events).json, "event_destroy_error");
    });
    run("every_required_marker", [&] {
      for (int family = 0; family < 4; ++family) {
        const int count = std::array{6, 9, 11, 5}[family];
        for (int point = 0; point < count; ++point) {
          FakeEvents events;
          Options options;
          if (family == 0) options.omit_step = point;
          if (family == 1) options.omit_host = point;
          if (family == 2) options.omit_gpu = point;
          if (family == 3) options.omit_expert = point;
          Invalid(One(events, options).json);
        }
      }
    });
    run("layers_and_shapes", [&] {
      for (int mode = 0; mode < 3; ++mode) {
        FakeEvents events;
        Options options;
        options.omit_layer = mode == 0 ? 47 : -1;
        options.duplicate_layer = mode == 1;
        options.rows = mode == 2 ? 1 : 4;
        Invalid(One(events, options).json);
      }
    });
    run("counts_and_stream_metadata", [&] {
      for (int mode = 0; mode < 7; ++mode) {
        FakeEvents events;
        Options options;
        options.missing_counts = mode == 0;
        options.missing_streams = mode == 1;
        options.bad_counts = mode == 2;
        options.duplicate_counts = mode == 3;
        options.counts_k = mode == 4 ? 9 : 10;
        options.streams = mode == 5 ? 0 : 4;
        options.wrong_stream = mode == 6;
        Invalid(One(events, options).json);
      }
    });
    run("absent_expert_cannot_record", [&] {
      FakeEvents events;
      Options options;
      options.distribution = 1;
      options.absent_ordinal = true;
      Invalid(One(events, options).json, "expert_metadata");
    });
    run("request_capacity_and_unsupported", [&] {
      FakeEvents events;
      const auto result = Scenario(
          events,
          [&](auto& timing) {
            Require(timing.BeginStep(1024, 3) != nullptr, "first step");
            Require(timing.BeginStep(1025, 3) == nullptr, "capacity exhausted");
          },
          1);
      Invalid(result.json, "step_capacity");
      for (int mode = 0; mode < 3; ++mode) {
        FakeEvents absent;
        const auto bad = Scenario(
            absent,
            [&](auto& timing) {
              Require(timing.BeginStep(1024, 3) == nullptr, "unsupported step");
            },
            mode == 0 ? 0 : 4, mode == 1 ? 2 : 3, mode != 2);
        Invalid(bad.json, "unsupported_request");
        Require(absent.created == 0 && absent.records == 0,
                "unsupported request creates no CUDA events");
      }
      FakeEvents capped;
      const auto large = Scenario(capped, [](auto&) {}, 4096);
      Require(large.json.GetInt("step_capacity") == 1024,
              "hard capacity bound");
    });
    run("engine_delivery_and_request_counts", [&] {
      for (int mode = 0; mode < 3; ++mode) {
        FakeEvents events;
        Options options;
        options.accepted = mode == 0 ? 4 : 3;
        options.delivered = mode == 1 ? 5 : 4;
        Invalid(One(events, options, mode == 2 ? 3 : 4).json);
      }
      FakeEvents clipped;
      Options last;
      last.delivered = 3;
      Valid(Scenario(
                clipped,
                [&](auto& timing) {
                  Step(timing, 0, last);
                  timing.Finish(3, 1, false, "length", "none", true);
                },
                3)
                .json);
      FakeEvents middle;
      Invalid(Scenario(
                  middle,
                  [&](auto& timing) {
                    Step(timing, 0, last);
                    Step(timing, 1);
                    timing.Finish(7, 2, false, "length", "none", true);
                  },
                  8)
                  .json,
              "step_incomplete");
    });
    run("failure_and_duplicate_finish", [&] {
      for (int mode = 0; mode < 4; ++mode) {
        FakeEvents events;
        Invalid(Scenario(events,
                         [&](auto& timing) {
                           Step(timing);
                           timing.Finish(
                               4, mode == 0 ? 2 : 1, mode == 1, "length",
                               mode == 2 ? "fallback" : "none", mode != 3);
                         })
                    .json,
                "request_result_contract");
      }
      FakeEvents duplicate;
      Invalid(Scenario(duplicate,
                       [&](auto& timing) {
                         Step(timing);
                         timing.Finish(4, 1, false, "length", "none", true);
                         timing.Finish(4, 1, false, "length", "none", true);
                       })
                  .json,
              "duplicate_finish");
    });
    run("identity_escaping", [&] {
      FakeEvents events;
      const std::string id = "id\"\\\n\t\x01";
      const auto result = Scenario(
          events,
          [&](auto& timing) {
            Step(timing);
            timing.Finish(4, 1, false, "length", "none", true);
          },
          4, 3, true, id);
      Valid(result.json);
      Require(result.json.GetString("response_id") == id,
              "escaped identity round trip");
    });
  } catch (const std::exception& error) {
    std::fprintf(stderr, "MTP_VERIFY_MOE_HOST_FAILURE %s\n", error.what());
    return 1;
  }
  std::printf("MTP_VERIFY_MOE_HOST_SUMMARY cases=%d passed=1 gpu_calls=0\n",
              cases);
  return 0;
}
