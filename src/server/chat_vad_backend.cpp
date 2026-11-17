#include "chat_server_internal.h"

#include <algorithm>
#include <new>

#include "object_protocol.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/vad_assets.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
using object_detail::Error;
const char* FailureCode(AsrFailure failure) {
  switch (failure) {
    case AsrFailure::kNone:
      return "none";
    case AsrFailure::kAssets:
      return "vad_assets_invalid";
    case AsrFailure::kTimeout:
      return "vad_assets_timeout";
    case AsrFailure::kWorker:
      return "vad_worker_unavailable";
    case AsrFailure::kResource:
      return "vad_resource_exhausted";
    case AsrFailure::kCompute:
      return "vad_compute_failed";
    case AsrFailure::kDevice:
      return "vad_state_invalid";
  }
  return "vad_state_invalid";
}
}  // namespace

std::string ChatServer::NativeVadHealthJson() const {
  const auto state = vad_health_.Snapshot();
  // This backend is CPU-only; a GPU monitor does not determine its health.
  auto result = SpeechHealthJson(state, true, "silero-vad-16k", FailureCode);
  result.pop_back();
  result += R"(,"host_weight_bytes":)" +
            std::to_string(state.ready ? audio::kVadWeightBytes : 0) +
            R"(,"asset_cleanup_pending":)";
  result += detail::IsolatedCleanupPending(IsolatedWorkerDomain::kModelAssets)
                ? "true}"
                : "false}";
  return result;
}

ObjectReply ChatServer::EnsureNativeVad(
    const BinaryAccess& access, std::shared_ptr<const audio::SileroVad>* out) {
  if (!out) return Error(500, "vad_owner_invalid", "VAD owner unavailable");
  const auto cancelled = [&] {
    return stop_requested_.load() || Clock::now() >= access.deadline ||
           (access.cancelled && access.cancelled());
  };
  const auto interrupted = [&] {
    return Error(409, "vad_cancelled", "VAD preparation cancelled");
  };
  if (vad_model_dir_.empty())
    return Error(503, "vad_not_configured", "Native VAD assets not configured");
  auto lock = LockUnlessCancelled(vad_mu_, cancelled);
  if (!lock.owns_lock() || cancelled()) return interrupted();
  switch (vad_health_.BeginLoad(true, Clock::now())) {
    case AsrLoadAdmission::kReady:
      *out = vad_;
      return {};
    case AsrLoadAdmission::kCooldown:
      return Error(503, "vad_retry_pending", "VAD recovery cooldown");
    case AsrLoadAdmission::kLoading:
      return Error(503, "vad_loading", "VAD load already active");
    case AsrLoadAdmission::kDisabled:
    case AsrLoadAdmission::kDevice:
      return Error(503, "vad_unavailable", "VAD backend unavailable");
    case AsrLoadAdmission::kStart:
      break;
  }
  ScopeExit loading([&] { vad_health_.CancelLoad(); });
  const auto fail = [&](AsrFailure kind, const char* why) {
    vad_health_.LoadFailed(kind, Clock::now());
    return Error(503, FailureCode(kind), why);
  };
  try {
    if (!runtime::ReadSystemMemory().CanAllocate(3 * audio::kVadFileBytes +
                                                 (8ULL << 20)))
      return fail(AsrFailure::kResource,
                  "VAD host memory admission unavailable");
    auto next = std::make_shared<audio::SileroVad>();
    GrammarControl control;
    control.cancelled = cancelled;
    control.compile_deadline = access.deadline;
    GrammarFailure failure;
    const auto loaded =
        LoadVadAssets(vad_model_dir_, next.get(), control, &failure);
    if (!loaded) {
      if (cancelled() || failure == GrammarFailure::kCancelled)
        return interrupted();
      const auto kind =
          failure == GrammarFailure::kResource     ? AsrFailure::kResource
          : failure == GrammarFailure::kTimeout    ? AsrFailure::kTimeout
          : failure == GrammarFailure::kDependency ? AsrFailure::kAssets
                                                   : AsrFailure::kWorker;
      return fail(kind, "VAD assets absent, invalid or unavailable");
    }
    if (cancelled()) return interrupted();
    vad_ = std::move(next);
    vad_health_.Loaded(true, 0);
    *out = vad_;
    return {};
  } catch (const std::bad_alloc&) {
    return fail(AsrFailure::kResource, "VAD host allocation exhausted");
  }
}

ObjectReply ChatServer::PrepareVadChunks(const AsrRequest& request,
                                         std::span<const float> pcm,
                                         const BinaryAccess& access,
                                         std::vector<audio::VadChunk>* out) {
  std::shared_ptr<const audio::SileroVad> model;
  auto reply = EnsureNativeVad(access, &model);
  if (!reply.ok()) return reply;
  const auto cancelled = [&] {
    return stop_requested_.load() || Clock::now() >= access.deadline ||
           (access.cancelled && access.cancelled());
  };
  const auto interrupted = [&] {
    return Error(409, "vad_cancelled", "VAD execution cancelled");
  };
  audio::VadChunkOptions options;
  options.threshold = request.chunking.threshold;
  options.prefix_padding_ms = request.chunking.prefix_padding_ms;
  options.silence_duration_ms = request.chunking.silence_duration_ms;
  options.max_samples = size_t(asr_limits_.max_audio_seconds) * 16000;
  const bool aligned = request.format == AsrFormat::kVerboseJson ||
                       request.format == AsrFormat::kSrt ||
                       request.format == AsrFormat::kVtt;
  options.max_chunk_samples =
      size_t(std::min({30, asr_limits_.max_audio_seconds,
                       aligned ? aligner_limits_.max_audio_seconds : 30})) *
      16000;
  options.max_chunks = vad_max_chunks_;
  try {
    std::vector<audio::VadFrame> frames;
    const auto scored =
        model->Score(pcm, options.max_samples, &frames, cancelled);
    if (!scored) {
      if (cancelled()) return interrupted();
      vad_health_.ComputeFailed(AsrFailure::kCompute, Clock::now());
      return Error(503, "vad_compute_failed", scored.message());
    }
    const auto split =
        audio::SplitVadChunks(frames, pcm.size(), options, out, cancelled);
    if (cancelled()) return interrupted();
    if (!split) return Error(400, "vad_chunk_limit", split.message());
    vad_health_.ComputeSucceeded();
    return {};
  } catch (const std::bad_alloc&) {
    vad_health_.ComputeFailed(AsrFailure::kResource, Clock::now());
    return Error(503, "vad_resource_exhausted", "VAD request memory exhausted");
  }
}
}  // namespace q4t::server
