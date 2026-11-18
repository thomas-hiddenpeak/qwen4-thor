#include "q4t/server/separator_service.h"

#include <limits>
#include <stdexcept>

#include "q4t/audio/separator_layout.h"
#include "q4t/io/json.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
constexpr auto kNow = Clock::time_point{} + 100s;
q4t::io::Json Parse(const std::string& text) {
  q4t::io::Json json;
  if (!q4t::io::ParseJson(text, &json)) throw std::runtime_error("health JSON");
  return json;
}
}  // namespace
Q4T_TEST(separator_resources_cover_full_duration_tracks_and_unified_load_peak) {
  SeparatorServiceLimits limits;
  SeparatorResourcePlan plan;
  Q4T_CHECK(PlanSeparatorServiceResources(limits, &plan));
  q4t::audio::SeparatorWorkspaceLayout maximum;
  Q4T_CHECK(
      q4t::audio::PlanSeparatorWorkspace(240000, 240000, 2ULL << 30, &maximum));
  Q4T_CHECK(plan.request_device_bytes == maximum.bytes);
  Q4T_CHECK(plan.resident_weight_bytes ==
            q4t::audio::SeparatorConfig::kWeightBytes);
  Q4T_CHECK(plan.load_peak_bytes >=
            3 * q4t::audio::SeparatorConfig::kFileBytes + (64ULL << 20));
  Q4T_CHECK(plan.request_host_bytes >=
            480000 * 8 * 4 + (8ULL << 20) + (4 * 160000 + 80000) * 4);
  const auto original = plan;
  limits.max_audio_seconds = 8;
  limits.workspace_mib = 512;
  Q4T_CHECK(PlanSeparatorServiceResources(limits, &plan));
  Q4T_CHECK(plan.request_device_bytes < original.request_device_bytes &&
            plan.request_host_bytes < original.request_host_bytes);
  auto json = Parse(SeparatorServiceLimitsJson(limits));
  Q4T_CHECK(json.GetInt("max_audio_seconds") == 8 &&
            json.GetInt("workspace_mib") == 512);
  return true;
}
Q4T_TEST(separator_resources_invalid_duration_and_budget_are_atomic) {
  SeparatorResourcePlan plan;
  Q4T_CHECK(PlanSeparatorServiceResources({}, &plan));
  const auto saved = plan;
  for (auto member : {&SeparatorServiceLimits::max_audio_seconds,
                      &SeparatorServiceLimits::workspace_mib}) {
    for (int value : {-1, 0, std::numeric_limits<int>::max()}) {
      SeparatorServiceLimits limits;
      limits.*member = value;
      Q4T_CHECK(!PlanSeparatorServiceResources(limits, &plan));
      Q4T_CHECK(plan.request_device_bytes == saved.request_device_bytes &&
                plan.request_host_bytes == saved.request_host_bytes);
    }
  }
  SeparatorServiceLimits limits;
  limits.max_audio_seconds = 31;
  Q4T_CHECK(!PlanSeparatorServiceResources(limits, &plan));
  limits = {};
  limits.workspace_mib = 512;
  Q4T_CHECK(!PlanSeparatorServiceResources(limits, &plan));
  Q4T_CHECK(plan.request_device_bytes == saved.request_device_bytes);
  Q4T_CHECK(!PlanSeparatorServiceResources({}, nullptr));
  return true;
}
Q4T_TEST(separator_health_assets_and_compute_failures_leave_asr_ready) {
  AsrHealth asr, separator;
  asr.Configure(true);
  Q4T_CHECK(asr.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  asr.Loaded(true, 1234);
  for (auto reason :
       {AsrFailure::kAssets, AsrFailure::kTimeout, AsrFailure::kWorker,
        AsrFailure::kResource, AsrFailure::kCompute}) {
    separator.Configure(true);
    Q4T_CHECK(separator.BeginLoad(false, kNow) == AsrLoadAdmission::kStart);
    separator.Loaded(false, 0);
    Q4T_CHECK(separator.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
    separator.LoadFailed(reason, kNow);
    const auto state = Parse(SeparatorHealthJson(separator.Snapshot(), true));
    Q4T_CHECK(state.GetString("model") ==
              q4t::audio::SeparatorConfig::kModelId);
    Q4T_CHECK(state.GetString("reason") == SeparatorFailureCode(reason) &&
              state.GetString("state") == "degraded");
    Q4T_CHECK(!state.GetBool("core_readiness_dependency", true));
    Q4T_CHECK(separator.BeginLoad(false, kNow) == AsrLoadAdmission::kReady);
    Q4T_CHECK(separator.BeginLoad(true, kNow + 5s - 1ns) ==
              AsrLoadAdmission::kCooldown);
    Q4T_CHECK(separator.BeginLoad(true, kNow + 5s) == AsrLoadAdmission::kStart);
    separator.Loaded(true, 2000);
    Q4T_CHECK(asr.Snapshot().ready &&
              asr.Snapshot().failure == AsrFailure::kNone);
    Q4T_CHECK(separator.Snapshot().ready);
  }
  return true;
}
Q4T_TEST(separator_health_cancel_and_sticky_device_protection_are_distinct) {
  AsrHealth separator;
  separator.Configure(true);
  Q4T_CHECK(separator.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  separator.CancelLoad();
  Q4T_CHECK(!separator.Snapshot().ready &&
            separator.Snapshot().load_failures == 0);
  Q4T_CHECK(separator.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  separator.Loaded(true, 1234);
  separator.ComputeFailed(AsrFailure::kDevice, kNow);
  separator.ComputeSucceeded();
  separator.Released();
  Q4T_CHECK(separator.BeginLoad(true, kNow + 1h) == AsrLoadAdmission::kDevice);
  Q4T_CHECK(separator.Snapshot().failure == AsrFailure::kDevice);
  Q4T_CHECK(Parse(SeparatorHealthJson(separator.Snapshot(), false))
                .GetString("reason") == "gpu_unhealthy");
  return true;
}
