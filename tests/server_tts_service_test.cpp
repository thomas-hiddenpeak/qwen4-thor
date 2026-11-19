#include "q4t/server/tts_service.h"

#include <algorithm>
#include <limits>
#include <stdexcept>

#include "q4t/audio/tts_codec_layout.h"
#include "q4t/audio/tts_generation.h"
#include "q4t/io/json.h"
#include "q4t/runtime/memory_budget.h"
#include "q4t/server/auxiliary_resources.h"
#include "q4t/server/chat_audio_output.h"
#include "q4t/server/client_policy.h"
#include "q4t/server/speech_output.h"
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
q4t::audio::TtsPrompt IclPrompt(size_t frames = 56) {
  using namespace q4t::audio;
  TtsPrompt prompt;
  const std::array<int32_t, 2> target{201, 202}, reference{101, 102};
  const std::vector<TtsCodes> codes(frames);
  const std::vector<float> speaker(2048, .25f);
  if (!PlanTtsBaseIclPrompt(target, reference, codes, {}, speaker, "auto", true,
                            1024, &prompt) ||
      !ValidateTtsPrompt(prompt))
    throw std::runtime_error("ICL prompt fixture");
  return prompt;
}
bool IclPolicy(ClientAdmission* admission, uint32_t tpm,
               uint32_t max_prompt = 8192) {
  GatewayClientPolicy policy;
  policy.version = 3;
  policy.gateway_token = std::string(64, 'a');
  policy.sha256 = std::string(64, '0');  // Owned in-memory fixture identity.
  policy.clients.push_back({.id = "audio",
                            .max_inflight = 1,
                            .max_control_inflight = 1,
                            .max_prompt_tokens = max_prompt,
                            .max_output_tokens = 1024,
                            .max_body_bytes = 1024,
                            .requests_per_minute = 16,
                            .tokens_per_minute = tpm});
  return admission->Configure(std::move(policy)).ok();
}
}  // namespace

Q4T_TEST(tts_icl_usage_charges_all_groups_independently_of_workspace_rows) {
  const auto prompt = IclPrompt();
  // 56 actual frames, each with 16 separately embedded codes. Reused PAD,
  // BOS and speaker vectors do not invent more text input IDs.
  Q4T_CHECK(prompt.text_ids.size() == 10 && prompt.InputTokens() == 906);
  Q4T_CHECK(prompt.prefill.size() == 70);
  TtsResourcePlan plan;
  Q4T_CHECK(PlanTtsServiceResources({}, prompt.text_ids.size(),
                                    prompt.prefill.size(), 160, &plan,
                                    q4t::audio::TtsVariant::kBase17B, 56));
  Q4T_CHECK(plan.generation_bytes > 0);
  Q4T_CHECK(IclPrompt(750).InputTokens() == 12010);
  // Existing non-reference prompts retain their prior text-only usage.
  q4t::audio::TtsPrompt base;
  const std::array<int32_t, 2> target{201, 202};
  const std::vector<float> speaker(2048, .25f);
  Q4T_CHECK(q4t::audio::PlanTtsBasePrompt(target, {}, speaker, "auto", true,
                                          128, &base));
  Q4T_CHECK(base.InputTokens() == base.text_ids.size());
  return true;
}

Q4T_TEST(tts_icl_rate_rejects_text_only_undercount_for_tpm_and_prompt_limits) {
  const auto prompt = IclPrompt();
  const auto now = ClientClock::now();
  for (bool prompt_limit : {false, true}) {
    ClientAdmission admission;
    Q4T_CHECK(IclPolicy(&admission, prompt_limit ? 10000 : 500,
                        prompt_limit ? 500 : 8192));
    const auto identity = admission.Authenticate("audio", std::string(64, 'a'));
    ClientRateLease old_count, complete_count;
    Q4T_CHECK(old_count.Reserve(admission, identity, prompt.text_ids.size(),
                                321, now) == ClientRateResult::kAllowed);
    old_count.Settle(now);
    Q4T_CHECK(
        complete_count.Reserve(admission, identity, prompt.InputTokens(), 321,
                               now) ==
        (prompt_limit ? ClientRateResult::kInvalid : ClientRateResult::kTpm));
    const auto rate = admission.RateUsage(0, now);
    Q4T_CHECK(rate.reserved == 0 && rate.tokens == 0 && rate.requests == 1);
  }
  return true;
}

Q4T_TEST(tts_icl_cancel_settles_only_started_reference_and_partial_output) {
  const auto prompt = IclPrompt();
  ClientAdmission admission;
  Q4T_CHECK(IclPolicy(&admission, 10000));
  const auto identity = admission.Authenticate("audio", std::string(64, 'a'));
  const auto now = ClientClock::now();
  {
    ClientRateLease queued;
    Q4T_CHECK(queued.Reserve(admission, identity, prompt.InputTokens(), 321,
                             now) == ClientRateResult::kAllowed);
  }
  Q4T_CHECK(admission.RateUsage(0, now).tokens == 0);
  {
    ClientRateLease started;
    Q4T_CHECK(started.Reserve(admission, identity, prompt.InputTokens(), 321,
                              now) == ClientRateResult::kAllowed);
    started.StartPrompt(prompt.InputTokens());
    started.Completion(7);  // Cancel during the first partially accepted frame.
    started.Settle(now);
    started.Settle(now);
  }
  const auto rate = admission.RateUsage(0, now);
  Q4T_CHECK(rate.tokens == 913 && rate.settled_tokens == 913 &&
            rate.reserved == 0 && rate.requests == 2);
  return true;
}

Q4T_TEST(
    tts_icl_chat_extension_admits_full_input_without_charging_cancelled_step) {
  const auto prompt = IclPrompt();
  const auto now = ClientClock::now();
  for (int scenario = 0; scenario < 4; ++scenario) {
    ClientAdmission admission;
    Q4T_CHECK(IclPolicy(&admission, scenario == 0 ? 500 : 10000,
                        scenario == 1 ? 900 : 8192));
    const auto identity = admission.Authenticate("audio", std::string(64, 'a'));
    ClientRateLease rate;
    Q4T_CHECK(rate.Reserve(admission, identity, 20, 321, now) ==
              ClientRateResult::kAllowed);
    rate.Start();
    rate.Completion(9);
    const auto extension =
        rate.ExtendPrompt(admission, identity, prompt.InputTokens(), now);
    Q4T_CHECK(extension == (scenario == 0   ? ClientRateResult::kTpm
                            : scenario == 1 ? ClientRateResult::kInvalid
                                            : ClientRateResult::kAllowed));
    if (scenario == 3) {
      rate.StartPrompt(20 + prompt.InputTokens());
      rate.Completion(9 + 17);
    }
    rate.Settle(now);
    const auto actual = admission.RateUsage(0, now);
    Q4T_CHECK(actual.requests == 1 && actual.reserved == 0 &&
              actual.tokens == (scenario == 3 ? 952 : 29));
  }
  return true;
}

Q4T_TEST(tts_icl_actual_usage_agrees_across_speech_chat_and_settlement) {
  const auto prompt = IclPrompt();
  ActualUsageCounter actual;
  actual.Enable();
  actual.BeginStep();
  actual.Observe({prompt.InputTokens(), 7});
  actual.Observe({prompt.InputTokens(), 33});
  actual.EndStep();
  const auto total = actual.Total();
  Q4T_CHECK(total && total->input == 906 && total->output == 33);
  std::string speech;
  Q4T_CHECK(SpeechAudioDone(*total, &speech));
  const auto done = Parse(speech.substr(speech.find("data: ") + 6));
  const auto* usage = done.Find("usage");
  Q4T_CHECK(usage && usage->GetInt("input_tokens") == 906 &&
            usage->GetInt("output_tokens") == 33 &&
            usage->GetInt("total_tokens") == 939);
  auto chat = Parse(R"({"prompt_tokens":20,"completion_tokens":9,
                       "total_tokens":29})");
  Q4T_CHECK(AddChatAudioUsage(*total, 32, &chat));
  Q4T_CHECK(chat.GetInt("prompt_tokens") == 926 &&
            chat.GetInt("completion_tokens") == 42 &&
            chat.GetInt("total_tokens") == 968);
  Q4T_CHECK(chat.Find("q4t_audio_synthesis")->GetInt("input_tokens") == 906 &&
            chat.Find("completion_tokens_details")->GetInt("audio_tokens") ==
                32);
  return true;
}

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

Q4T_TEST(tts_base_synthesis_resources_include_real_source_and_owned_condition) {
  TtsResourcePlan base, builtin;
  Q4T_CHECK(PlanTtsServiceResources({}, 32, 48, 160, &builtin));
  Q4T_CHECK(PlanTtsServiceResources({}, 32, 48, 160, &base,
                                    q4t::audio::TtsVariant::kBase17B));
  Q4T_CHECK(base.resident_weight_bytes == builtin.resident_weight_bytes);
  Q4T_CHECK(base.source_staging_bytes ==
            kNativeTtsBaseFileBytes + kNativeTtsCodecFileBytes);
  Q4T_CHECK(base.request_device_bytes == builtin.request_device_bytes + 8192);
  Q4T_CHECK(base.request_host_bytes == builtin.request_host_bytes + 16384);
  Q4T_CHECK(base.load_peak_bytes == builtin.load_peak_bytes +
                                        kNativeTtsBaseFileBytes -
                                        kNativeTtsFileBytes);
  const auto saved = base;
  Q4T_CHECK(!PlanTtsServiceResources({}, 32, 48, 160, &base,
                                     q4t::audio::TtsVariant::kCustomVoice06B));
  Q4T_CHECK(base.load_peak_bytes == saved.load_peak_bytes);
  return true;
}

Q4T_TEST(tts_voice_registration_resources_bound_features_without_talker) {
  TtsVoiceResourcePlan small, large;
  Q4T_CHECK(PlanTtsVoiceResources(1280, 300ULL << 20, &small));
  Q4T_CHECK(PlanTtsVoiceResources(1440000, 300ULL << 20, &large));
  Q4T_CHECK(small.frames == 5 && large.frames == 5625);
  Q4T_CHECK(small.device_weight_bytes == 48004352 &&
            large.device_weight_bytes == small.device_weight_bytes);
  Q4T_CHECK(large.device_workspace_bytes > small.device_workspace_bytes &&
            large.device_workspace_bytes < (300ULL << 20));
  Q4T_CHECK(large.host_feature_bytes > small.host_feature_bytes &&
            large.host_feature_bytes < (32ULL << 20));
  Q4T_CHECK(large.source_staging_bytes == kNativeTtsBaseFileBytes &&
            large.load_peak_bytes == small.load_peak_bytes &&
            large.load_peak_bytes > kNativeTtsBaseFileBytes + 48004352);
  const auto saved = large;
  for (size_t samples : {size_t{0}, size_t{1279}, size_t{1440001},
                         std::numeric_limits<size_t>::max()})
    Q4T_CHECK(!PlanTtsVoiceResources(samples, 300ULL << 20, &large));
  Q4T_CHECK(
      !PlanTtsVoiceResources(1280, small.device_workspace_bytes - 1, &large));
  Q4T_CHECK(!PlanTtsVoiceResources(1280, 300ULL << 20, nullptr));
  Q4T_CHECK(large.frames == saved.frames &&
            large.device_workspace_bytes == saved.device_workspace_bytes);
  return true;
}


Q4T_TEST(tts_voice_icl_resource_peak_covers_both_sources_and_graphs) {
  TtsVoiceResourcePlan old, icl;
  Q4T_CHECK(PlanTtsVoiceResources(55693, 2ULL << 30, &old));
  Q4T_CHECK(PlanTtsVoiceResources(55693, 2ULL << 30, &icl, true));
  Q4T_CHECK(icl.device_weight_bytes == 48004352 + 224937216);
  Q4T_CHECK(kNativeTtsVoiceEncoderOffset % 256 == 0 &&
            kNativeTtsVoiceEncoderOffset >=
                q4t::audio::TtsSpeakerConfig::kDeviceBytes);
  Q4T_CHECK(icl.source_staging_bytes ==
            kNativeTtsBaseFileBytes + kNativeTtsCodecFileBytes);
  Q4T_CHECK(icl.load_peak_bytes ==
            old.load_peak_bytes + 224937216 + kNativeTtsCodecFileBytes);
  Q4T_CHECK(icl.reference_frames == 30 &&
            icl.reference_host_bytes == 2 * 30 * 16 * sizeof(int32_t));
  Q4T_CHECK(icl.host_feature_bytes ==
            old.host_feature_bytes + icl.reference_host_bytes);
  return true;
}

Q4T_TEST(tts_voice_icl_workspace_is_sequential_max_for_actual_pcm) {
  for (size_t samples : {1280, 1920, 1921, 55693, 1440000}) {
    TtsVoiceResourcePlan legacy, actual;
    q4t::audio::TtsEncoderWorkspace encoder;
    Q4T_CHECK(PlanTtsVoiceResources(samples, 2ULL << 30, &legacy));
    Q4T_CHECK(PlanTtsVoiceResources(samples, 2ULL << 30, &actual, true));
    Q4T_CHECK(q4t::audio::PlanTtsEncoderWorkspace(samples, 2ULL << 30,
                                                  &encoder));
    Q4T_CHECK(actual.speaker_workspace_bytes == legacy.device_workspace_bytes);
    Q4T_CHECK(actual.encoder_workspace_bytes == encoder.bytes);
    Q4T_CHECK(actual.device_workspace_bytes ==
              std::max(legacy.device_workspace_bytes, encoder.bytes));
    Q4T_CHECK(actual.device_workspace_bytes <
              legacy.device_workspace_bytes + encoder.bytes);
    Q4T_CHECK(actual.reference_frames == (samples + 1919) / 1920);
  }
  return true;
}

Q4T_TEST(tts_voice_icl_budget_and_sample_failures_leave_plan_unchanged) {
  TtsVoiceResourcePlan plan;
  Q4T_CHECK(PlanTtsVoiceResources(1440000, 2ULL << 30, &plan, true));
  const auto saved = plan;
  Q4T_CHECK(!PlanTtsVoiceResources(1440000,
                                    plan.device_workspace_bytes - 1, &plan,
                                    true));
  for (size_t samples : {size_t{0}, size_t{1279}, size_t{1440001},
                         std::numeric_limits<size_t>::max()})
    Q4T_CHECK(!PlanTtsVoiceResources(samples, 2ULL << 30, &plan, true));
  Q4T_CHECK(!PlanTtsVoiceResources(1280, 2ULL << 30, nullptr, true));
  Q4T_CHECK(plan.device_weight_bytes == saved.device_weight_bytes &&
            plan.device_workspace_bytes == saved.device_workspace_bytes &&
            plan.reference_frames == saved.reference_frames &&
            plan.host_feature_bytes == saved.host_feature_bytes);
  return true;
}
