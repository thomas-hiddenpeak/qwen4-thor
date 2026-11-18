#include "q4t/server/tts_service.h"

#include <limits>
#include <stdexcept>

#include "q4t/audio/tts_codec_layout.h"
#include "q4t/io/json.h"
#include "q4t/server/tts_assets.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
constexpr auto kNow = Clock::time_point{} + 100s;
q4t::io::Json Parse(const std::string& text) {
  q4t::io::Json value;
  if (!q4t::io::ParseJson(text, &value)) throw std::runtime_error("TTS JSON");
  return value;
}
}  // namespace

Q4T_TEST(tts_resources_cover_real_fp32_expansion_and_request_dimensions) {
  TtsServiceLimits limits;
  TtsResourcePlan plan;
  Q4T_CHECK(PlanTtsServiceResources(limits, 32, 48, 160, &plan));
  Q4T_CHECK(plan.resident_weight_bytes == 8123997956ULL);
  Q4T_CHECK(plan.source_staging_bytes == 4515695644ULL);
  Q4T_CHECK(plan.possible_file_page_bytes == plan.source_staging_bytes);
  Q4T_CHECK(plan.expansion_scratch_bytes == (6ULL << 20));
  Q4T_CHECK(plan.load_peak_bytes >=
            8123997956ULL + 2 * 4515695644ULL + (518ULL << 20));
  Q4T_CHECK(plan.request_device_bytes ==
            plan.generation_bytes + plan.codec_bytes);
  Q4T_CHECK(plan.request_host_bytes >=
            160ULL * 1920 * 28 + (400ULL << 20) + (2ULL << 30));
  const auto small = plan;
  Q4T_CHECK(PlanTtsServiceResources(limits, limits.max_text_tokens,
                                    limits.max_prefill_tokens,
                                    limits.max_frames, &plan));
  Q4T_CHECK(plan.request_device_bytes > small.request_device_bytes &&
            plan.request_host_bytes > small.request_host_bytes &&
            plan.request_device_bytes <= (8192ULL << 20));
  Q4T_CHECK(plan.load_peak_bytes == small.load_peak_bytes);
  // A small compressed-output budget cannot erase its large PCM16 input.
  limits.max_output_mib = 1;
  Q4T_CHECK(PlanTtsServiceResources(limits, 32, 48, 4096, &plan));
  Q4T_CHECK(plan.request_host_bytes >=
            4096ULL * 1920 * 28 + (22ULL << 20) + (2ULL << 30));
  limits.max_output_mib = 64;
  const auto json = Parse(TtsServiceLimitsJson(limits));
  Q4T_CHECK(json.GetInt("max_frames") == 4096 &&
            json.GetInt("max_output_mib") == 64);
  return true;
}

Q4T_TEST(tts_resources_reject_capacity_and_overflow_without_trimming) {
  TtsResourcePlan plan;
  Q4T_CHECK(PlanTtsServiceResources({}, 32, 48, 160, &plan));
  const auto saved = plan;
  for (auto member :
       {&TtsServiceLimits::max_text_tokens,
        &TtsServiceLimits::max_prefill_tokens, &TtsServiceLimits::max_frames,
        &TtsServiceLimits::workspace_mib, &TtsServiceLimits::max_output_mib}) {
    for (int value : {-1, 0, std::numeric_limits<int>::max()}) {
      TtsServiceLimits limits;
      limits.*member = value;
      Q4T_CHECK(!PlanTtsServiceResources(limits, 32, 48, 160, &plan));
      Q4T_CHECK(plan.request_device_bytes == saved.request_device_bytes &&
                plan.load_peak_bytes == saved.load_peak_bytes);
    }
  }
  for (size_t dimension = 0; dimension < 3; ++dimension) {
    for (size_t value : {size_t{0}, std::numeric_limits<size_t>::max()}) {
      std::array<size_t, 3> dims{32, 48, 160};
      dims[dimension] = value;
      Q4T_CHECK(!PlanTtsServiceResources({}, dims[0], dims[1], dims[2], &plan));
    }
  }
  TtsServiceLimits limits;
  limits.workspace_mib = 128;
  Q4T_CHECK(!PlanTtsServiceResources(limits, 32, 48, 160, &plan));
  limits = {};
  limits.max_prefill_tokens = 32768;
  Q4T_CHECK(!limits.Valid());
  Q4T_CHECK(!PlanTtsServiceResources({}, 32, 48, 160, nullptr));
  Q4T_CHECK(plan.request_device_bytes == saved.request_device_bytes);
  return true;
}

Q4T_TEST(tts_health_auxiliary_failures_do_not_change_ready_asr) {
  AsrHealth tts, asr;
  asr.Configure(true);
  Q4T_CHECK(asr.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  asr.Loaded(true, 123);
  for (auto reason :
       {AsrFailure::kAssets, AsrFailure::kTimeout, AsrFailure::kWorker,
        AsrFailure::kResource, AsrFailure::kCompute}) {
    tts.Configure(true);
    Q4T_CHECK(tts.BeginLoad(false, kNow) == AsrLoadAdmission::kStart);
    tts.Loaded(false, 0);
    Q4T_CHECK(tts.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
    tts.LoadFailed(reason, kNow);
    const auto health = Parse(TtsHealthJson(tts.Snapshot(), true));
    Q4T_CHECK(health.GetString("state") == "degraded" &&
              health.GetString("reason") == TtsFailureCode(reason) &&
              !health.GetBool("core_readiness_dependency", true));
    Q4T_CHECK(tts.BeginLoad(false, kNow) == AsrLoadAdmission::kReady);
    Q4T_CHECK(tts.BeginLoad(true, kNow + 4999ms) ==
              AsrLoadAdmission::kCooldown);
    Q4T_CHECK(tts.BeginLoad(true, kNow + 5s) == AsrLoadAdmission::kStart);
    tts.Loaded(true, 8123997956ULL);
    Q4T_CHECK(Parse(TtsHealthJson(tts.Snapshot(), true)).GetString("state") ==
              "ready");
    Q4T_CHECK(asr.Snapshot().ready && asr.Snapshot().load_failures == 0 &&
              asr.Snapshot().failure == AsrFailure::kNone);
  }
  return true;
}

Q4T_TEST(tts_health_cancellation_release_and_device_fault_boundaries) {
  AsrHealth tts;
  tts.Configure(true);
  Q4T_CHECK(tts.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  tts.CancelLoad();
  Q4T_CHECK(tts.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  tts.Loaded(true, 8123997956ULL);
  tts.Released();
  Q4T_CHECK(!tts.Snapshot().ready && tts.Snapshot().device_bytes == 0);
  Q4T_CHECK(tts.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  tts.LoadFailed(AsrFailure::kDevice, kNow);
  tts.ComputeSucceeded();
  tts.Released();
  Q4T_CHECK(tts.BeginLoad(true, kNow + 1h) == AsrLoadAdmission::kDevice);
  Q4T_CHECK(Parse(TtsHealthJson(tts.Snapshot(), true)).GetString("reason") ==
            "gpu_unhealthy");
  AsrHealth metadata;
  metadata.Configure(true);
  Q4T_CHECK(
      Parse(TtsHealthJson(metadata.Snapshot(), false)).GetString("reason") ==
      "gpu_unhealthy");
  Q4T_CHECK(!metadata.Snapshot().ready);
  return true;
}
