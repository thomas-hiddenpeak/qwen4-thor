#include "q4t/server/aligner_service.h"

#include <limits>

#include "q4t/audio/aligner_config.h"
#include "q4t/io/json.h"
#include "q4t/server/aligner_assets.h"
#include "q4t/server/server_options.h"
#include "q4t/server/chat_capabilities.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using C = q4t::audio::AlignerConfig;
using Clock = std::chrono::steady_clock;
const auto kNow = Clock::time_point{} + 100s;
const std::vector<q4t::audio::AlignerWord> kWords{{"hello", 0, 5},
                                                  {"world", 6, 11}};
q4t::Status Encode(std::string_view text, std::vector<uint32_t>* ids) {
  *ids = {text == "hello" ? 71U : 86U, 42U};
  return {};
}
}  // namespace

Q4T_TEST(aligner_resources_include_class_logits_and_unified_load_peak) {
  AlignerServiceLimits limits;
  AlignerResourcePlan plan;
  Q4T_CHECK(PlanAlignerServiceResources(limits, &plan));
  Q4T_CHECK(plan.load_peak_bytes >=
            2 * kNativeAlignerFileBytes + (512ULL << 20));
  Q4T_CHECK(plan.request_host_bytes >=
            (73ULL << 20) + 512 * 2 * 5000 * sizeof(uint16_t));
  Q4T_CHECK(plan.request_device_bytes ==
            plan.encoder_bytes + plan.decoder_bytes);
  Q4T_CHECK(plan.encoder_bytes && plan.decoder_bytes &&
            plan.request_device_bytes <= (1024ULL << 20));
  const auto saved = plan;
  limits.workspace_mib = 64;
  Q4T_CHECK(!PlanAlignerServiceResources(limits, &plan));
  Q4T_CHECK(plan.request_device_bytes == saved.request_device_bytes);
  return true;
}

Q4T_TEST(aligner_resources_reject_untrained_duration_and_invalid_limits) {
  AlignerServiceLimits limits;
  AlignerResourcePlan plan;
  for (int value : {0, -1, 401, std::numeric_limits<int>::max()}) {
    limits.max_audio_seconds = value;
    Q4T_CHECK(!PlanAlignerServiceResources(limits, &plan));
  }
  limits.max_audio_seconds = 400;
  limits.max_context_tokens = 8192;
  limits.workspace_mib = 8192;
  Q4T_CHECK(PlanAlignerServiceResources(limits, &plan));
  limits.max_words = 2048;  // 400 seconds + 2048 words cannot fit this context.
  Q4T_CHECK(!PlanAlignerServiceResources(limits, &plan));
  limits = {};
  limits.max_words = 0;
  Q4T_CHECK(!PlanAlignerServiceResources(limits, &plan));
  limits = {};
  limits.max_context_tokens = 65536;
  Q4T_CHECK(!PlanAlignerServiceResources(limits, &plan));
  return true;
}

Q4T_TEST(aligner_health_failure_and_retry_leave_transcription_ready) {
  AsrHealth transcription, alignment;
  transcription.Configure(true);
  alignment.Configure(true);
  Q4T_CHECK(transcription.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  transcription.Loaded(true, 1000);
  for (auto reason :
       {AsrFailure::kAssets, AsrFailure::kTimeout, AsrFailure::kWorker,
        AsrFailure::kResource, AsrFailure::kCompute}) {
    alignment.Configure(true);
    Q4T_CHECK(alignment.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
    alignment.LoadFailed(reason, kNow);
    q4t::io::Json state;
    Q4T_CHECK(q4t::io::ParseJson(AlignerHealthJson(alignment.Snapshot(), true),
                                 &state));
    Q4T_CHECK(state.GetString("model") == C::kModelId);
    Q4T_CHECK(state.GetString("reason") == AlignerFailureCode(reason));
    Q4T_CHECK(state.GetString("state") == "degraded");
    Q4T_CHECK(!state.GetBool("core_readiness_dependency", true));
    Q4T_CHECK(alignment.BeginLoad(true, kNow + 5s - 1ns) ==
              AsrLoadAdmission::kCooldown);
    Q4T_CHECK(alignment.BeginLoad(true, kNow + 5s) == AsrLoadAdmission::kStart);
    alignment.Loaded(true, 2000);
    Q4T_CHECK(transcription.Snapshot().ready &&
              transcription.Snapshot().load_failures == 0 &&
              transcription.Snapshot().failure == AsrFailure::kNone);
    Q4T_CHECK(alignment.Snapshot().ready);
  }
  return true;
}

Q4T_TEST(aligner_health_cancellation_and_shared_device_protection) {
  AsrHealth h;
  h.Configure(true);
  Q4T_CHECK(h.BeginLoad(false, kNow) == AsrLoadAdmission::kStart);
  h.Loaded(false, 0);
  Q4T_CHECK(h.BeginLoad(true, kNow) == AsrLoadAdmission::kStart);
  h.CancelLoad();
  Q4T_CHECK(h.Snapshot().metadata_ready && h.Snapshot().load_failures == 0);
  q4t::io::Json state;
  Q4T_CHECK(q4t::io::ParseJson(AlignerHealthJson(h.Snapshot(), false), &state));
  Q4T_CHECK(state.GetString("reason") == "gpu_unhealthy");
  Q4T_CHECK(h.Snapshot().failure == AsrFailure::kNone);
  h.ComputeFailed(AsrFailure::kDevice, kNow);
  h.ComputeSucceeded();
  h.Released();
  Q4T_CHECK(h.BeginLoad(true, kNow + 24h) == AsrLoadAdmission::kDevice);
  return true;
}

Q4T_TEST(aligner_prompt_preserves_literal_words_and_actual_slot_positions) {
  AlignerPrompt prompt;
  Q4T_CHECK(BuildAlignerPrompt(kWords, 2, {}, Encode, &prompt));
  Q4T_CHECK(prompt.tokens ==
            std::vector<int32_t>({C::kAudioStart, C::kAudioPad, C::kAudioPad,
                                  C::kAudioEnd, 71, 42, C::kTimestampToken,
                                  C::kTimestampToken, 86, 42,
                                  C::kTimestampToken, C::kTimestampToken}));
  Q4T_CHECK(prompt.positions == std::vector<int32_t>({6, 7, 10, 11}));
  Q4T_CHECK(prompt.audio_tokens == 2);
  return true;
}

Q4T_TEST(aligner_prompt_budget_marker_and_cancel_fail_atomically) {
  AlignerPrompt prompt;
  prompt.tokens = {42};
  const auto reject = [&](const AlignerEncode& encode,
                          const std::function<bool()>& cancelled = {}) {
    return !BuildAlignerPrompt(kWords, 2, {}, encode, &prompt, cancelled) &&
           prompt.tokens == std::vector<int32_t>{42};
  };
  Q4T_CHECK(reject([](auto, auto* ids) {
    *ids = {C::kTimestampToken};
    return q4t::Status{};
  }));
  Q4T_CHECK(reject([](auto, auto* ids) {
    ids->resize(4096, 1);
    return q4t::Status{};
  }));
  Q4T_CHECK(reject([](auto, auto*) { return q4t::Status{}; }));
  int calls = 0;
  Q4T_CHECK(reject(Encode, [&] { return ++calls == 3; }));
  Q4T_CHECK(!BuildAlignerPrompt({}, 2, {}, Encode, &prompt));
  AlignerServiceLimits limits;
  limits.max_words = 1;
  Q4T_CHECK(!BuildAlignerPrompt(kWords, 2, limits, Encode, &prompt));
  Q4T_CHECK(prompt.tokens == std::vector<int32_t>{42});
  return true;
}

Q4T_TEST(aligner_classes_choose_first_bf16_maximum_with_true_row_stride) {
  std::vector<uint16_t> logits(4 * C::kClasses, 0xbf80);  // -1.
  logits[36] = logits[37] = 0x3f80;
  logits[C::kClasses + 4999] = 0x4000;
  logits[2 * C::kClasses + 1] = 0;  // 0 beats the other negative logits.
  std::vector<uint32_t> classes;
  Q4T_CHECK(SelectAlignerClasses(logits, 2, &classes));
  Q4T_CHECK(classes == std::vector<uint32_t>({36, 4999, 1, 0}));
  return true;
}

Q4T_TEST(aligner_classes_nonfinite_shape_and_cancel_preserve_prior_owner) {
  std::vector<uint16_t> logits(2 * C::kClasses, 0);
  std::vector<uint32_t> classes{42};
  for (auto invalid : {uint16_t{0x7fc0}, uint16_t{0x7f80}, uint16_t{0xff80}}) {
    logits.back() = invalid;
    Q4T_CHECK(!SelectAlignerClasses(logits, 1, &classes));
    Q4T_CHECK(classes == std::vector<uint32_t>{42});
  }
  logits.back() = 0;
  int calls = 0;
  Q4T_CHECK(
      !SelectAlignerClasses(logits, 1, &classes, [&] { return ++calls == 2; }));
  Q4T_CHECK(!SelectAlignerClasses(logits, 2, &classes));
  Q4T_CHECK(!SelectAlignerClasses({}, 0, &classes));
  Q4T_CHECK(classes == std::vector<uint32_t>{42});
  return true;
}

Q4T_TEST(aligner_options_bind_optional_assets_and_preserve_atomic_limits) {
  ServerOptions opts;
  opts.model_dir = "/main-model";
  const auto parse = [&](std::initializer_list<std::string_view> args) {
    return ParseServerOptions({args.begin(), args.size()}, &opts);
  };
  Q4T_CHECK(!parse({"--aligner-model-dir", "/alignment"}));
  Q4T_CHECK(opts.aligner_model_dir.empty());
  Q4T_CHECK(
      parse({"--asr-model-dir", "/asr", "--aligner-model-dir", "/alignment"}));
  for (const char* path : {"relative", "/", "/a/", "/a//b", "/a/../b"})
    Q4T_CHECK(!parse({"--aligner-model-dir", path}));
  Q4T_CHECK(opts.aligner_model_dir == "/alignment");
  Q4T_CHECK(
      parse({"--aligner-max-audio-seconds", "400",
             "--aligner-max-context-tokens", "8192", "--aligner-max-words",
             "128", "--aligner-workspace-mib", "8192"}));
  Q4T_CHECK(!parse({"--aligner-max-audio-seconds", "401"}));
  Q4T_CHECK(!parse({"--aligner-max-words", "0"}));
  Q4T_CHECK(!parse({"--aligner-workspace-mib", "8193"}));
  Q4T_CHECK(opts.aligner_limits.max_audio_seconds == 400 &&
            opts.aligner_limits.max_words == 128);
  return true;
}

Q4T_TEST(aligner_capability_separates_configuration_and_unfinished_segments) {
  for (bool enabled : {false, true}) {
    q4t::io::Json root;
    Q4T_CHECK(
        q4t::io::ParseJson(ChatCapabilitiesJson("chat", false, true, 0, nullptr,
                                                false, true, enabled),
                           &root));
    const auto* asr = root.Find("transcriptions");
    const auto* alignment = asr ? asr->Find("alignment") : nullptr;
    Q4T_CHECK(
        alignment && alignment->GetBool("configured", !enabled) == enabled &&
        alignment->GetString("profile") == kNativeAlignerProfile &&
        alignment->GetString("weight_sha256") == kNativeAlignerWeightSha256);
    Q4T_CHECK(asr->GetArray("remaining_native_work"));
    Q4T_CHECK(root.GetBool("implementation_only", false));
    Q4T_CHECK(!root.GetBool("release_attestation", true));
  }
  return true;
}
