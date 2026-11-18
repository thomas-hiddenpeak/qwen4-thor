#include "q4t/server/asr_service.h"

#include <limits>

#include "q4t/io/json.h"
#include "q4t/server/server_options.h"
#include "q4t/server/asr_assets.h"
#include "q4t/server/chat_capabilities.h"
#include "q4t/server/request_limits.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
const auto kNow = Clock::time_point{} + 100s;
q4t::io::Json Health(const AsrHealth& health, bool gpu = true) {
  q4t::io::Json result;
  if (!q4t::io::ParseJson(AsrHealthJson(health.Snapshot(), gpu), &result))
    throw std::runtime_error("invalid ASR health JSON");
  return result;
}
}  // namespace

Q4T_TEST(asr_capability_binds_fixed_backend_when_configured_or_disabled) {
  for (const bool configured : {false, true}) {
    q4t::io::Json root;
    Q4T_CHECK(
        q4t::io::ParseJson(ChatCapabilitiesJson("chat", false, true, 0, nullptr,
                                                false, configured),
                           &root));
    const auto* value = root.Find("transcriptions");
    Q4T_CHECK(value && value->GetString("profile") == kNativeAsrProfile &&
              value->GetString("model") == "qwen3-asr-0.6b" &&
              value->GetString("revision") == kNativeAsrRevision &&
              value->GetString("weight_sha256") == kNativeAsrWeightSha256 &&
              value->GetBool("configured", !configured) == configured);
    const auto* chat = value->Find("chat_input");
    Q4T_CHECK(chat && chat->GetString("profile") == "q4t-chat-audio-input-v1" &&
              chat->GetString("preparation") == "native_asr_transcript" &&
              chat->GetInt("max_parts") == 8);
  }
  return true;
}

Q4T_TEST(asr_health_configured_metadata_and_device_publication) {
  AsrHealth h;
  Q4T_CHECK(h.BeginLoad(true, kNow) == AsrLoadAdmission::kDisabled);
  Q4T_CHECK(Health(h).GetString("state") == "disabled");
  h.Configure(true);
  h.Loaded(true, 123);  // No started operation may publish readiness.
  Q4T_CHECK(!h.Snapshot().ready && h.Snapshot().device_bytes == 0);
  Q4T_CHECK(h.BeginLoad(false, kNow) == AsrLoadAdmission::kStart);
  Q4T_CHECK(h.BeginLoad(true, kNow) == AsrLoadAdmission::kLoading);
  Q4T_CHECK(Health(h).GetString("state") == "loading");
  h.Loaded(false, 0);
  Q4T_CHECK(Health(h).GetString("state") == "metadata_ready");
  Q4T_CHECK(h.BeginLoad(false, kNow) == AsrLoadAdmission::kReady);
  Q4T_CHECK(h.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  h.Loaded(true, 1876017152);
  const auto state = h.Snapshot();
  Q4T_CHECK(state.ready && state.metadata_ready && !state.loading);
  Q4T_CHECK(state.device_bytes == 1876017152 && state.load_attempts == 2);
  Q4T_CHECK(Health(h).GetString("state") == "ready");
  return true;
}

Q4T_TEST(asr_health_failed_assets_cooldown_and_retry_have_exact_boundaries) {
  AsrHealth h;
  h.Configure(true);
  Q4T_CHECK(h.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  h.LoadFailed(AsrFailure::kAssets, kNow);
  Q4T_CHECK(h.Snapshot().load_failures == 1);
  Q4T_CHECK(Health(h).GetString("reason") == "asr_assets_invalid");
  Q4T_CHECK(Health(h).GetString("state") == "degraded");
  Q4T_CHECK(h.BeginLoad(true, kNow + 5s - 1ns) == AsrLoadAdmission::kCooldown);
  Q4T_CHECK(h.Snapshot().load_attempts == 1);
  Q4T_CHECK(h.BeginLoad(true, kNow + 5s) == AsrLoadAdmission::kStart);
  h.Loaded(true, 1876017152);
  Q4T_CHECK(Health(h).GetString("reason") == "none");
  Q4T_CHECK(h.Snapshot().load_attempts == 2 && h.Snapshot().load_failures == 1);
  return true;
}

Q4T_TEST(asr_health_cancellation_preserves_metadata_without_failure_charge) {
  AsrHealth h;
  h.Configure(true);
  Q4T_CHECK(h.BeginLoad(false, kNow) == AsrLoadAdmission::kStart);
  h.Loaded(false, 0);
  Q4T_CHECK(h.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  h.CancelLoad();
  Q4T_CHECK(h.Snapshot().metadata_ready && !h.Snapshot().ready);
  Q4T_CHECK(h.Snapshot().load_failures == 0 && !h.Snapshot().loading);
  Q4T_CHECK(h.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  h.LoadFailed(AsrFailure::kResource, kNow);
  Q4T_CHECK(h.BeginLoad(false, kNow) == AsrLoadAdmission::kReady);
  Q4T_CHECK(h.BeginLoad(true, kNow) == AsrLoadAdmission::kCooldown);
  Q4T_CHECK(h.Snapshot().load_failures == 1);
  return true;
}

Q4T_TEST(asr_health_compute_fault_recovers_with_valid_weights_after_cooldown) {
  AsrHealth h;
  h.Configure(true);
  Q4T_CHECK(h.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  h.Loaded(true, 1000);
  h.ComputeFailed(AsrFailure::kCompute, kNow);
  Q4T_CHECK(h.Snapshot().ready && h.Snapshot().device_bytes == 1000);
  Q4T_CHECK(h.BeginLoad(true, kNow + 4s) == AsrLoadAdmission::kCooldown);
  Q4T_CHECK(h.BeginLoad(true, kNow + 5s) == AsrLoadAdmission::kReady);
  Q4T_CHECK(h.Snapshot().load_attempts == 1);
  h.ComputeSucceeded();
  Q4T_CHECK(Health(h).GetString("state") == "ready");
  Q4T_CHECK(h.Snapshot().compute_failures == 1 &&
            h.Snapshot().failure == AsrFailure::kNone);
  return true;
}

Q4T_TEST(asr_health_device_failure_is_sticky_across_success_and_retry) {
  AsrHealth h;
  h.Configure(true);
  Q4T_CHECK(h.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  h.LoadFailed(AsrFailure::kDevice, kNow);
  h.ComputeSucceeded();
  h.CancelLoad();
  h.Loaded(true, 1000);
  Q4T_CHECK(h.BeginLoad(true, kNow + 24h) == AsrLoadAdmission::kDevice);
  Q4T_CHECK(Health(h).GetString("reason") == "gpu_unhealthy");
  Q4T_CHECK(!h.Snapshot().ready && h.Snapshot().device_bytes == 0);
  h.ComputeFailed(AsrFailure::kResource, kNow + 24h);
  Q4T_CHECK(h.Snapshot().failure == AsrFailure::kDevice);
  return true;
}

Q4T_TEST(asr_health_auxiliary_reasons_do_not_claim_core_dependency) {
  AsrHealth h;
  for (const auto reason : {AsrFailure::kAssets, AsrFailure::kTimeout,
                            AsrFailure::kWorker, AsrFailure::kResource}) {
    h.Configure(true);
    Q4T_CHECK(h.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
    h.LoadFailed(reason, kNow);
    const auto json = Health(h);
    Q4T_CHECK(json.GetString("reason") == AsrFailureCode(reason));
    const auto* dependency = json.Find("core_readiness_dependency");
    Q4T_CHECK(dependency && dependency->IsBool() && !dependency->boolean);
    Q4T_CHECK(Health(h, false).GetString("reason") == "gpu_unhealthy");
    Q4T_CHECK(h.Snapshot().failure == reason);  // Projection cannot mutate it.
  }
  return true;
}

Q4T_TEST(asr_health_release_clears_device_residency_without_erasing_history) {
  AsrHealth h;
  h.Configure(true);
  Q4T_CHECK(h.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  h.Loaded(true, 1000);
  h.Released();
  Q4T_CHECK(!h.Snapshot().ready && h.Snapshot().device_bytes == 0);
  Q4T_CHECK(h.Snapshot().metadata_ready && h.Snapshot().load_attempts == 1);
  Q4T_CHECK(Health(h).GetString("state") == "metadata_ready");
  Q4T_CHECK(h.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  h.LoadFailed(AsrFailure::kDevice, kNow);
  h.Released();
  Q4T_CHECK(h.Snapshot().failure == AsrFailure::kDevice);
  return true;
}

Q4T_TEST(asr_resources_account_unified_peak_and_reject_small_workspace) {
  AsrServiceLimits limits;
  AsrResourcePlan plan;
  Q4T_CHECK(PlanAsrServiceResources(limits, &plan));
  Q4T_CHECK(plan.load_peak_bytes >= 2 * 1876091704ULL + (512ULL << 20));
  Q4T_CHECK(plan.request_host_bytes > (75ULL << 20) + 480000 * sizeof(float));
  Q4T_CHECK(plan.shared_host_bytes ==
            6 * kAggregateChatBodyBytes + 2 * kChatBodyBytes);
  Q4T_CHECK(plan.request_device_bytes > plan.encoder_bytes &&
            plan.request_device_bytes > plan.decoder_bytes);
  Q4T_CHECK(plan.request_device_bytes <= (1024ULL << 20));
  const auto saved = plan;
  AsrResourcePlan many;
  Q4T_CHECK(PlanAsrServiceResources(limits, &many, 8));
  Q4T_CHECK(many.request_host_bytes == plan.request_host_bytes &&
            many.request_device_bytes == plan.request_device_bytes &&
            many.shared_host_bytes ==
                plan.shared_host_bytes + 14 * kChatBodyBytes);
  Q4T_CHECK(!PlanAsrServiceResources(limits, &many, 0));
  Q4T_CHECK(!PlanAsrServiceResources(limits, &many, 9));
  limits.workspace_mib = 64;
  Q4T_CHECK(!PlanAsrServiceResources(limits, &plan));
  Q4T_CHECK(plan.request_device_bytes == saved.request_device_bytes &&
            plan.load_peak_bytes == saved.load_peak_bytes);
  return true;
}

Q4T_TEST(asr_resources_reject_invalid_capacity_without_trimming_audio) {
  AsrResourcePlan plan;
  for (int value : {-1, 0, 601, std::numeric_limits<int>::max()}) {
    AsrServiceLimits limits;
    limits.max_audio_seconds = value;
    Q4T_CHECK(!PlanAsrServiceResources(limits, &plan));
  }
  AsrServiceLimits limits;
  limits.max_audio_seconds = 600;
  Q4T_CHECK(!PlanAsrServiceResources(limits, &plan));
  limits.max_context_tokens = 8192;
  limits.max_output_tokens = 128;
  limits.workspace_mib = 8192;
  Q4T_CHECK(PlanAsrServiceResources(limits, &plan));
  Q4T_CHECK(limits.max_audio_seconds == 600 &&
            limits.max_context_tokens == 8192 &&
            limits.max_output_tokens == 128);
  limits.max_context_tokens = 65536;
  Q4T_CHECK(!PlanAsrServiceResources(limits, &plan));
  return true;
}

Q4T_TEST(asr_options_bind_distinct_model_and_validate_atomic_updates) {
  ServerOptions opts;
  opts.model_dir = "/read-only-model";
  const auto parse = [&](std::initializer_list<std::string_view> args) {
    return ParseServerOptions({args.begin(), args.size()}, &opts);
  };
  Q4T_CHECK(parse({"--asr-model-dir", "/missing/optional-asr"}));
  for (const char* path : {"relative", "/", "/a/", "/a//b", "/a/../b"})
    Q4T_CHECK(!parse({"--asr-model-dir", path}));
  Q4T_CHECK(opts.asr_model_dir == "/missing/optional-asr");
  Q4T_CHECK(!parse({"--served-model-name", "qwen3-asr-0.6b"}));
  Q4T_CHECK(!parse({"--model-alias", "qwen3-asr-0.6b"}));
  Q4T_CHECK(!parse({"--asr-max-audio-seconds", "600"}));
  Q4T_CHECK(opts.asr_limits.max_audio_seconds == 30);
  Q4T_CHECK(parse({"--asr-max-audio-seconds", "600", "--asr-max-context-tokens",
                   "8192", "--asr-max-output-tokens", "128",
                   "--asr-workspace-mib", "8192"}));
  Q4T_CHECK(opts.asr_limits.max_audio_seconds == 600);
  Q4T_CHECK(!parse({"--asr-workspace-mib", "8193"}));
  Q4T_CHECK(!parse({"--asr-max-output-tokens", "0"}));
  Q4T_CHECK(!parse({"--asr-max-context-tokens", "65536"}));
  Q4T_CHECK(opts.asr_limits.workspace_mib == 8192 &&
            opts.asr_limits.max_output_tokens == 128);
  q4t::io::Json value;
  Q4T_CHECK(q4t::io::ParseJson(AsrServiceLimitsJson(opts.asr_limits), &value));
  Q4T_CHECK(value.GetInt("max_audio_seconds") == 600 &&
            value.GetInt("max_context_tokens") == 8192 &&
            value.GetInt("max_output_tokens") == 128);
  return true;
}
