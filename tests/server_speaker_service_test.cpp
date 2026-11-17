#include "q4t/server/speaker_service.h"

#include <limits>
#include <stdexcept>

#include "q4t/audio/speaker_layout.h"
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
Q4T_TEST(speaker_resources_cover_full_known_reference_and_unified_load_peak) {
  SpeakerServiceLimits limits;
  SpeakerResourcePlan plan;
  Q4T_CHECK(PlanSpeakerServiceResources(limits, &plan));
  q4t::audio::SpeakerWorkspaceLayout reference;
  Q4T_CHECK(
      q4t::audio::PlanSpeakerWorkspace(1001, 1001, 128ULL << 20, &reference));
  Q4T_CHECK(plan.request_device_bytes >= reference.bytes);
  Q4T_CHECK(plan.resident_weight_bytes ==
            q4t::audio::SpeakerConfig::kWeightBytes);
  Q4T_CHECK(plan.load_peak_bytes >=
            3 * q4t::audio::SpeakerConfig::kFileBytes + (64ULL << 20));
  Q4T_CHECK(plan.request_host_bytes >= 4 * 160000 * 4 + (16ULL << 20));
  const auto original = plan;
  limits.window_ms = 30000;
  limits.workspace_mib = 512;
  Q4T_CHECK(PlanSpeakerServiceResources(limits, &plan));
  Q4T_CHECK(plan.request_device_bytes > original.request_device_bytes &&
            plan.request_host_bytes > original.request_host_bytes);
  auto json = Parse(SpeakerServiceLimitsJson(limits));
  Q4T_CHECK(json.GetInt("window_ms") == 30000 &&
            json.GetInt("max_windows") == limits.max_windows);
  return true;
}
Q4T_TEST(speaker_resources_invalid_counts_intervals_and_budget_are_atomic) {
  SpeakerResourcePlan plan;
  Q4T_CHECK(PlanSpeakerServiceResources({}, &plan));
  const auto saved = plan;
  for (auto member :
       {&SpeakerServiceLimits::window_ms, &SpeakerServiceLimits::step_ms,
        &SpeakerServiceLimits::max_windows, &SpeakerServiceLimits::max_speakers,
        &SpeakerServiceLimits::workspace_mib}) {
    for (int value : {-1, 0, std::numeric_limits<int>::max()}) {
      SpeakerServiceLimits limits;
      limits.*member = value;
      Q4T_CHECK(!PlanSpeakerServiceResources(limits, &plan));
      Q4T_CHECK(plan.request_device_bytes == saved.request_device_bytes &&
                plan.request_host_bytes == saved.request_host_bytes);
    }
  }
  SpeakerServiceLimits limits;
  limits.step_ms = 3001;
  Q4T_CHECK(!PlanSpeakerServiceResources(limits, &plan));
  limits = {};
  limits.workspace_mib = 16;
  Q4T_CHECK(!PlanSpeakerServiceResources(limits, &plan));
  Q4T_CHECK(plan.request_device_bytes == saved.request_device_bytes);
  Q4T_CHECK(!PlanSpeakerServiceResources({}, nullptr));
  return true;
}
Q4T_TEST(speaker_health_assets_and_compute_failures_leave_asr_ready) {
  AsrHealth asr, speaker;
  asr.Configure(true);
  Q4T_CHECK(asr.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  asr.Loaded(true, 1234);
  for (auto reason :
       {AsrFailure::kAssets, AsrFailure::kTimeout, AsrFailure::kWorker,
        AsrFailure::kResource, AsrFailure::kCompute}) {
    speaker.Configure(true);
    Q4T_CHECK(speaker.BeginLoad(false, kNow) == AsrLoadAdmission::kStart);
    speaker.Loaded(false, 0);
    Q4T_CHECK(speaker.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
    speaker.LoadFailed(reason, kNow);
    const auto state = Parse(SpeakerHealthJson(speaker.Snapshot(), true));
    Q4T_CHECK(state.GetString("model") == q4t::audio::SpeakerConfig::kModelId);
    Q4T_CHECK(state.GetString("reason") == SpeakerFailureCode(reason) &&
              state.GetString("state") == "degraded");
    Q4T_CHECK(!state.GetBool("core_readiness_dependency", true));
    Q4T_CHECK(speaker.BeginLoad(false, kNow) == AsrLoadAdmission::kReady);
    Q4T_CHECK(speaker.BeginLoad(true, kNow + 5s - 1ns) ==
              AsrLoadAdmission::kCooldown);
    Q4T_CHECK(speaker.BeginLoad(true, kNow + 5s) == AsrLoadAdmission::kStart);
    speaker.Loaded(true, 2000);
    Q4T_CHECK(asr.Snapshot().ready &&
              asr.Snapshot().failure == AsrFailure::kNone);
    Q4T_CHECK(speaker.Snapshot().ready);
  }
  return true;
}
Q4T_TEST(speaker_health_cancel_and_sticky_device_protection_are_distinct) {
  AsrHealth speaker;
  speaker.Configure(true);
  Q4T_CHECK(speaker.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  speaker.CancelLoad();
  Q4T_CHECK(!speaker.Snapshot().ready && speaker.Snapshot().load_failures == 0);
  Q4T_CHECK(speaker.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  speaker.Loaded(true, 1234);
  speaker.ComputeFailed(AsrFailure::kDevice, kNow);
  speaker.ComputeSucceeded();
  speaker.Released();
  Q4T_CHECK(speaker.BeginLoad(true, kNow + 1h) == AsrLoadAdmission::kDevice);
  Q4T_CHECK(speaker.Snapshot().failure == AsrFailure::kDevice);
  Q4T_CHECK(
      Parse(SpeakerHealthJson(speaker.Snapshot(), false)).GetString("reason") ==
      "gpu_unhealthy");
  return true;
}
