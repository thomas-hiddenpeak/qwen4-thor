#include "q4t/server/tts_service.h"

#include <limits>
#include <stdexcept>

#include "q4t/audio/tts_codec_layout.h"
#include "q4t/io/json.h"
#include "q4t/runtime/memory_budget.h"
#include "q4t/server/auxiliary_resources.h"
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
  Q4T_CHECK(plan.possible_file_page_bytes == 0);
  Q4T_CHECK(plan.expansion_scratch_bytes == (6ULL << 20));
  Q4T_CHECK(plan.load_peak_bytes >=
            8123997956ULL + 4515695644ULL + (518ULL << 20));
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

Q4T_TEST(auxiliary_resources_keep_residents_pending_and_one_transient) {
  AuxiliaryResourceBudget plan;
  Q4T_CHECK(plan.Add(100, 20, 60, 40));
  Q4T_CHECK(plan.Add(200, 30, 10, 70));
  Q4T_CHECK(plan.resident_bytes == 300 && plan.pending_host_bytes == 50 &&
            plan.sequence_bytes == 70);
  Q4T_CHECK(!plan.Add(std::numeric_limits<uint64_t>::max(), 0, 0, 0));
  Q4T_CHECK(plan.resident_bytes == 300 && plan.pending_host_bytes == 50 &&
            plan.sequence_bytes == 70);
  Q4T_CHECK(!plan.Add(0, std::numeric_limits<uint64_t>::max() - 50, 0, 0));
  return true;
}

namespace {
q4t::runtime::BudgetRequest AuxiliaryBudgetRequest(int slots) {
  q4t::runtime::BudgetRequest request;
  request.max_len = 1024;
  request.max_seq = slots;
  request.max_prefill = 2048;
  request.mem_fraction = 1;
  request.mtp_workspace_bytes = 1ULL << 30;
  request.mtp_weights_bytes = 6ULL << 30;
  request.mtp_load_host_bytes = 4ULL << 30;
  return request;
}
}  // namespace

Q4T_TEST(auxiliary_budget_single_slot_excludes_primary_request_overlap) {
  using namespace q4t::runtime;
  BudgetModelParams params;
  params.has_mtp = true;
  auto request = AuxiliaryBudgetRequest(1);
  const auto primary =
      ComputeMemoryBudget(params, request, 1ULL << 30, 128ULL << 30);
  request.auxiliary_sequence_bytes = 5ULL << 30;
  const auto combined =
      ComputeMemoryBudget(params, request, 1ULL << 30, 128ULL << 30);
  Q4T_CHECK(primary.feasible && combined.feasible);
  Q4T_CHECK(combined.per_request == request.auxiliary_sequence_bytes);
  Q4T_CHECK(combined.fixed == primary.fixed &&
            combined.state_pool == primary.state_pool &&
            combined.startup_peak == primary.startup_peak);
  const auto exact = ComputeMemoryBudget(params, request, 1ULL << 30,
                                         combined.estimated_total);
  const auto short_one = ComputeMemoryBudget(params, request, 1ULL << 30,
                                             combined.estimated_total - 1);
  Q4T_CHECK(exact.feasible && !short_one.feasible);
  return true;
}

Q4T_TEST(auxiliary_budget_multiple_slots_keep_primary_overlap) {
  using namespace q4t::runtime;
  BudgetModelParams params;
  params.has_mtp = true;
  auto request = AuxiliaryBudgetRequest(2);
  const auto primary =
      ComputeMemoryBudget(params, request, 1ULL << 30, 128ULL << 30);
  request.auxiliary_sequence_bytes = 5ULL << 30;
  const auto combined =
      ComputeMemoryBudget(params, request, 1ULL << 30, 128ULL << 30);
  Q4T_CHECK(primary.feasible && combined.feasible && combined.max_seq == 2);
  Q4T_CHECK(combined.per_request == primary.per_request + (5ULL << 30));
  return true;
}

Q4T_TEST(auxiliary_budget_plain_decode_still_charges_optional_loads) {
  using namespace q4t::runtime;
  auto request = AuxiliaryBudgetRequest(1);
  const auto primary =
      ComputeMemoryBudget({}, request, 1ULL << 30, 128ULL << 30);
  request.auxiliary_sequence_bytes = 5ULL << 30;
  const auto combined =
      ComputeMemoryBudget({}, request, 1ULL << 30, 128ULL << 30);
  Q4T_CHECK(primary.feasible && combined.feasible && primary.per_request == 0);
  Q4T_CHECK(combined.estimated_total == primary.estimated_total + (5ULL << 30));
  request.auxiliary_sequence_bytes = std::numeric_limits<size_t>::max();
  Q4T_CHECK(
      !ComputeMemoryBudget({}, request, 1ULL << 30, 128ULL << 30).feasible);
  return true;
}

Q4T_TEST(auxiliary_weight_groups_retain_metadata_workers_and_pending_bodies) {
  AuxiliaryResourceBudget plan;
  Q4T_CHECK(plan.Add(100 + 12, 20, 60, 40));
  Q4T_CHECK(plan.Add(80 + 13, 30, 10, 70));
  Q4T_CHECK(plan.ShareWeights(100, 80));
  Q4T_CHECK(plan.resident_bytes == 100 + 12 + 13 &&
            plan.pending_host_bytes == 50 && plan.sequence_bytes == 70);
  Q4T_CHECK(!plan.ShareWeights(100, 80));
  Q4T_CHECK(plan.resident_bytes == 125);
  return true;
}
