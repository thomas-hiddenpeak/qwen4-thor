#include "chat_server_internal.h"

#include <algorithm>
#include <new>

#include "cuda_memory_backend.h"
#include "object_protocol.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/request_diagnostics.h"
#include "q4t/server/tts_assets.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
using object_detail::Error;
constexpr uint64_t kHostMargin = 512ULL << 20;
}  // namespace

AsrHealth& ChatServer::TtsHealth(audio::TtsVariant variant) {
  return variant == audio::TtsVariant::kBase17B ? tts_base_health_
                                                : tts_health_;
}

std::string ChatServer::NativeTtsHealthJson(audio::TtsVariant variant) const {
  const bool base = variant == audio::TtsVariant::kBase17B;
  auto result = SpeechHealthJson(
      (base ? tts_base_health_ : tts_health_).Snapshot(), gpu_healthy_.load(),
      base ? kNativeTtsBaseModel : kNativeTtsModel, TtsFailureCode);
  result.pop_back();
  result += R"(,"asset_cleanup_pending":)";
  result += detail::IsolatedCleanupPending(IsolatedWorkerDomain::kModelAssets)
                ? "true}"
                : "false}";
  return result;
}

ObjectReply ChatServer::ActivateAuxiliaryFamily(
    bool speech_output, const std::function<bool()>& cancelled) {
  const auto interrupted = [&] {
    return Error(409, "auxiliary_cancelled", "Optional model switch cancelled");
  };
  const auto release = [&](std::mutex& owner, uint8_t*& memory,
                           const char* site, const auto& retired) {
    auto lock = LockUnlessCancelled(owner, cancelled);
    if (!lock.owns_lock() || cancelled()) return interrupted();
    if (!memory) return ObjectReply{};
    auto gpu = LockUnlessCancelled(model_mu_, cancelled);
    if (!gpu.owns_lock() || cancelled()) return interrupted();
    const auto freed = detail::ReleaseCudaMemory(memory, site);
    RecordDeviceMemoryFailure(freed, site);
    retired(freed.ok());
    if (!freed.ok()) {
      AbortBackendExecution();
      return Error(503, "gpu_unhealthy", "Optional weight release failed");
    }
    return ObjectReply{};
  };
  // The other family has no executing borrower: every optional load/run
  // owns auxiliary_mu_. Metadata-only requests use the owner mutex but no
  // device pointer. All compute streams drain before releasing that lease.
  if (!speech_output) {
    auto owner = LockUnlessCancelled(tts_mu_, cancelled);
    if (!owner.owns_lock() || cancelled()) return interrupted();
    return ReleaseNativeTts(cancelled);
  }
  auto reply = release(embedding_mu_, d_embedding_weights_,
                       "native_embedding.family_release", [&](bool ok) {
                         embedding_weights_ = {};
                         embedding_ready_.store(false);
                         embedding_device_bytes_.store(0);
                         if (!ok)
                           embedding_failure_.store(EmbeddingFailure::kDevice);
                       });
  if (!reply.ok()) return reply;
  const auto speech = [&](std::mutex& owner, uint8_t*& pointer, auto& weights,
                          AsrHealth& health, const char* site) {
    return release(owner, pointer, site, [&](bool ok) {
      weights = {};
      health.Released();
      if (!ok) health.ComputeFailed(AsrFailure::kDevice, Clock::now());
    });
  };
  reply = speech(asr_mu_, d_asr_weights_, asr_weights_, asr_health_,
                 "native_asr.family_release");
  if (!reply.ok()) return reply;
  reply = speech(aligner_mu_, d_aligner_weights_, aligner_weights_,
                 aligner_health_, "native_aligner.family_release");
  if (!reply.ok()) return reply;
  reply = speech(speaker_mu_, d_speaker_weights_, speaker_weights_,
                 speaker_health_, "native_speaker.family_release");
  if (!reply.ok()) return reply;
  reply = speech(separator_mu_, d_separator_weights_, separator_weights_,
                 separator_health_, "native_separator.family_release");
  if (!reply.ok()) return reply;
  return speech(voice_mu_, d_voice_weights_, voice_weights_, voice_health_,
                "native_voice.family_release");
}

ObjectReply ChatServer::ReleaseNativeTts(
    const std::function<bool()>& cancelled) {
  if (!d_tts_talker_ && !d_tts_codec_) return {};
  auto gpu = LockUnlessCancelled(model_mu_, cancelled);
  if (!gpu.owns_lock() || cancelled())
    return Error(409, "tts_cancelled", "TTS model switch cancelled");
  auto& health = TtsHealth(tts_weights_.talker.variant);
  bool clean = true;
  for (auto** pointer : {&d_tts_talker_, &d_tts_codec_}) {
    const auto freed =
        detail::ReleaseCudaMemory(*pointer, "native_tts.family_release");
    RecordDeviceMemoryFailure(freed, "native_tts.family_release");
    clean &= freed.ok();
  }
  tts_weights_ = {};
  health.Released();
  if (!clean) {
    health.ComputeFailed(AsrFailure::kDevice, Clock::now());
    AbortBackendExecution();
    return Error(503, "gpu_unhealthy", "Optional weight release failed");
  }
  return {};
}

ObjectReply ChatServer::EnsureNativeTts(bool weights,
                                        const BinaryAccess& access,
                                        RequestDiagnostics* diagnostic,
                                        audio::TtsVariant variant) {
  if (variant != audio::TtsVariant::kCustomVoice17B &&
      variant != audio::TtsVariant::kBase17B)
    return Error(400, "tts_variant_invalid", "TTS variant is unavailable");
  const bool base = variant == audio::TtsVariant::kBase17B;
  auto& health = TtsHealth(variant);
  auto& tokenizer = base ? tts_base_tok_ : tts_tok_;
  const auto& directory = base ? tts_base_model_dir_ : tts_model_dir_;
  const auto cancelled = [&] {
    return stop_requested_.load() || Clock::now() >= access.deadline ||
           (access.cancelled && access.cancelled());
  };
  const auto interrupted = [&] {
    return Error(409, "tts_cancelled", "TTS asset load cancelled");
  };
  if (directory.empty())
    return Error(404, "model_not_found", "TTS model is not configured");
  auto lock = LockUnlessCancelled(tts_mu_, cancelled);
  if (!lock.owns_lock() || cancelled()) return interrupted();
  // Every device caller owns auxiliary_mu_. A metadata request cannot evict
  // a model. Switching retires the old pair before allocating the new one.
  if (weights && (d_tts_talker_ || d_tts_codec_) &&
      tts_weights_.talker.variant != variant) {
    auto retired = ReleaseNativeTts(cancelled);
    if (!retired.ok()) return retired;
  }
  switch (health.BeginLoad(weights, Clock::now())) {
    case AsrLoadAdmission::kReady:
      return {};
    case AsrLoadAdmission::kDisabled:
      return Error(404, "model_not_found", "TTS model is not configured");
    case AsrLoadAdmission::kCooldown:
      return Error(503, "tts_retry_pending", "TTS recovery cooldown");
    case AsrLoadAdmission::kLoading:
      return Error(503, "tts_loading", "TTS load already active");
    case AsrLoadAdmission::kDevice:
      return Error(503, "gpu_unhealthy", "Shared GPU is unavailable");
    case AsrLoadAdmission::kStart:
      break;
  }
  ScopeExit loading([&] { health.CancelLoad(); });
  const auto fail = [&](AsrFailure kind, const char* message) {
    health.LoadFailed(kind, Clock::now());
    return Error(503, TtsFailureCode(kind), message);
  };
  try {
    const uint64_t estimate =
        kHostMargin +
        (weights ? ((base ? kNativeTtsBaseFileBytes : kNativeTtsFileBytes) +
                    kNativeTtsCodecFileBytes) +
                       kNativeTtsWeightBytes +
                       audio::TtsConfig::kCodecDecoderBytes + (6ULL << 20)
                 : 0);
    if (!runtime::ReadSystemMemory().CanAllocate(estimate))
      return fail(AsrFailure::kResource, "TTS unified memory unavailable");
    TtsHostAssets assets;
    GrammarFailure cause;
    GrammarControl control;
    control.cancelled = cancelled;
    control.compile_deadline = std::min(
        access.deadline,
        Clock::now() + std::chrono::milliseconds(kModelAssetTimeoutMs));
    if (!LoadTtsAssets(directory, &assets, control, &cause, weights,
                       kTtsAssetStagingBytes, {variant, true})) {
      if (cancelled() || cause == GrammarFailure::kCancelled)
        return interrupted();
      if (cause == GrammarFailure::kResource)
        return fail(AsrFailure::kResource, "TTS asset resources unavailable");
      if (cause == GrammarFailure::kTimeout)
        return fail(AsrFailure::kTimeout, "TTS asset load timed out");
      if (cause == GrammarFailure::kDependency)
        return fail(AsrFailure::kAssets, "TTS assets fail identity checks");
      return fail(AsrFailure::kWorker, "TTS asset worker unavailable");
    }
    if (cancelled()) return interrupted();
    if (weights) {
      auto gpu = LockUnlessCancelled(model_mu_, cancelled);
      if (!gpu.owns_lock() || cancelled()) return interrupted();
      const auto shared = ObserveHealth();
      if (!shared.gpu_healthy)
        return fail(AsrFailure::kDevice, "Shared GPU is unavailable");
      if (!shared.Healthy())
        return Error(503, shared.ErrorCode(), "Primary backend is unavailable");
      if (!runtime::ReadSystemMemory().CanAllocate(
              kNativeTtsWeightBytes + audio::TtsConfig::kCodecDecoderBytes +
              kHostMargin + (6ULL << 20)))
        return fail(AsrFailure::kResource, "TTS device memory unavailable");
      std::unique_ptr<RequestDiagnostics::ExecutionScope> execution;
      if (diagnostic)
        execution = std::make_unique<RequestDiagnostics::ExecutionScope>(
            *diagnostic, ProgressStage::kCache);
      uint8_t* talker = nullptr;
      uint8_t* codec = nullptr;
      ScopeExit cleanup([&] {
        for (auto** pointer : {&talker, &codec}) {
          const auto freed =
              detail::ReleaseCudaMemory(*pointer, "native_tts.weights_cleanup");
          RecordDeviceMemoryFailure(freed, "native_tts.weights_cleanup");
          if (!freed.ok()) {
            health.ComputeFailed(AsrFailure::kDevice, Clock::now());
            AbortBackendExecution();
          }
        }
      });
      for (const auto& [pointer, bytes] :
           {std::pair{&talker, kNativeTtsWeightBytes},
            std::pair{&codec, size_t(audio::TtsConfig::kCodecDecoderBytes)}}) {
        const auto memory =
            detail::AllocateCudaMemory(*pointer, bytes, "native_tts.weights");
        RecordDeviceMemoryFailure(memory, "native_tts.weights");
        if (!memory.ok()) {
          if (!memory.exhausted()) AbortBackendExecution();
          return fail(
              memory.exhausted() ? AsrFailure::kResource : AsrFailure::kDevice,
              "TTS weight allocation unavailable");
        }
      }
      TtsBoundWeights bound;
      const auto upload = UploadTtsWeights(
          assets, {talker, kNativeTtsWeightBytes},
          {codec, audio::TtsConfig::kCodecDecoderBytes}, &bound, cancelled);
      if (!upload.ok()) {
        if (upload.device_error != cudaSuccess) {
          AbortBackendExecution();
          return fail(AsrFailure::kDevice, "TTS weight upload failed");
        }
        if (cancelled()) return interrupted();
        return fail(AsrFailure::kAssets, "TTS weight binding failed");
      }
      if (execution) execution->Completed();
      if (cancelled()) return interrupted();
      d_tts_talker_ = talker;
      d_tts_codec_ = codec;
      talker = codec = nullptr;
      tts_weights_ = bound;
    }
    if (!tokenizer) tokenizer = std::move(assets.tokenizer);
    health.Loaded(weights, weights ? kNativeTtsWeightBytes +
                                         audio::TtsConfig::kCodecDecoderBytes
                                   : 0);
    return {};
  } catch (const std::bad_alloc&) {
    return fail(AsrFailure::kResource, "TTS host allocation exhausted");
  }
}
}  // namespace q4t::server
