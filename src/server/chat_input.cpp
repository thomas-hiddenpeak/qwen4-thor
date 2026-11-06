// Existing ChatServer responsibilities; shared state stays in ChatServer.
#include "chat_server_internal.h"
#include "cuda_memory_backend.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <string>
#include <utility>
#include <vector>
#include <cuda_bf16.h>
#include <cuda_runtime.h>
#include "q4t/server/chat_template.h"
#include "q4t/server/media_admission.h"
#include "q4t/vision/forward_timing.h"
#include "q4t/vision/processor.h"
#include "q4t/vision/timing.h"
#include "q4t/vision/vision.h"

namespace q4t::server {
bool ChatServer::RunVisionPipeline(
    const std::vector<VisionItem>& items, const vision::MediaPlan& plan,
    uint16_t** out_feats, int* out_num_tokens, std::vector<int>* out_counts,
    std::vector<std::array<int, 3>>* out_grids, std::string* err,
    DeviceMemoryResult* device_result, const char* request_id,
    const std::function<bool()>& cancelled, ChatPlanError* media_error) {
  using vision::TimingScope;
  using vision::TimingStage;
  vision::StageTimings timings(vision::VisionTimingEnabled());
  auto* timing = timings.Active();
  bool timing_completed = false;
  // Declared before buffer owners, so terminal reporting includes cleanup.
  ScopeExit report_timing([&] {
    if (!timing) return;
    size_t frames = 0;
    for (const auto& item : items) frames += item.frames.size();
    timings.Report(stderr, request_id, timing_completed, items.size(), frames,
                   plan.patches);
  });
  const runtime::CleanupPhase phase("vision.request");
  *device_result = {};
  *out_feats = nullptr;
  *out_num_tokens = 0;
  out_counts->clear();
  if (out_grids) out_grids->clear();
  if (vision_tower_ == nullptr) {
    if (err) *err = "vision tower not loaded (text-only model)";
    return false;
  }
  if (items.empty()) return true;  // nothing to do
  const auto fail = [&](const char* code, size_t item, size_t frame = 0) {
    media_error->code = code;
    media_error->param =
        item < items.size() ? MediaInputParam(items[item], frame) : "messages";
    if (err) *err = code;
    return false;
  };
  if (plan.item_count != items.size())
    return fail("media_plan_mismatch", items.size());
  if (cancelled()) return fail("request_cancelled", 0);

  const vision::VisionConfig& cfg = vision_tower_->cfg;
  const int m = cfg.spatial_merge_size;

  // 1. Process each item (CPU: decode + resize + normalize + patchify).
  //    Images use the image budget (proc_cfg_); videos use the video budget
  //    (video_proc_cfg_). Both produce the same per-patch layout [C,T,P,P]
  //    (1536 floats at P=16,T=2) so they share one VisionForward batch.
  std::vector<std::vector<float>> pixel_sets;  // per-item float32 patches
  pixel_sets.reserve(items.size());
  std::vector<vision::ImageShape> shapes;
  shapes.reserve(items.size());
  int total_L = 0;
  for (size_t index = 0; index < items.size(); ++index) {
    const auto& item = items[index];
    if (cancelled()) return fail("request_cancelled", index);
    vision::ProcessorResult processed;
    const vision::ProcessorOptions options{
        .cancelled =
            [](void* context) {
              return (*static_cast<const std::function<bool()>*>(context))();
            },
        .cancel_context = const_cast<std::function<bool()>*>(&cancelled),
        .admission = &plan.items[index],
        .result = &processed,
        .timings = timing};
    std::vector<float> pix;
    vision::ImageShape sh;
    std::string perr;
    if (item.kind == VisionItem::kImage) {
      vision::ProcessedImage pi;
      if (!vision::ProcessImage(
              reinterpret_cast<const uint8_t*>(item.frames[0].data()),
              item.frames[0].size(), proc_cfg_, &pi, &perr, options)) {
        return fail(processed.code, index, processed.frame);
      }
      pix = std::move(pi.pixel_values);
      sh.h = pi.grid_h;
      sh.w = pi.grid_w;
      sh.t = pi.grid_t;
    } else {
      std::vector<const uint8_t*> fptrs;
      std::vector<size_t> flens;
      fptrs.reserve(item.frames.size());
      flens.reserve(item.frames.size());
      for (const auto& f : item.frames) {
        fptrs.push_back(reinterpret_cast<const uint8_t*>(f.data()));
        flens.push_back(f.size());
      }
      vision::ProcessedVideo pv;
      if (!vision::ProcessVideo(fptrs, flens, video_proc_cfg_, &pv, &perr,
                                options)) {
        return fail(processed.code, index, processed.frame);
      }
      pix = std::move(pv.pixel_values);
      sh.h = pv.grid_h;
      sh.w = pv.grid_w;
      sh.t = pv.grid_t;
    }
    const auto& expected = plan.items[index].geometry;
    if (sh.h != expected.grid_h || sh.w != expected.grid_w ||
        sh.t != expected.grid_t || pix.size() != expected.pixel_elements)
      return fail("media_plan_mismatch", index);
    shapes.push_back(sh);
    total_L += sh.L();
    pixel_sets.push_back(std::move(pix));
    // Patch-unit grid (t, h, w) for the 3D MRoPE table (same order as the
    // feature rows / placeholders).
    if (out_grids) {
      // Feature rows remain in ViT time-major order. Each temporal group is
      // a separate prompt block, including its own timestamp and text clock.
      for (int group = 0; group < sh.t; ++group)
        out_grids->push_back({1, sh.h, sh.w});
    }
  }
  if (static_cast<uint64_t>(total_L) != plan.patches)
    return fail("media_plan_mismatch", items.size());
  if (cancelled()) return fail("request_cancelled", items.size());

  // 2. Build the device pixel buffer (float32 -> BF16, concatenated).
  const int patch_dim = cfg.in_channels * cfg.temporal_patch_size *
                        cfg.patch_size * cfg.patch_size;  // 1536
  TimingScope pack_timing(timing, TimingStage::kPack);
  std::vector<uint16_t> pixels_bf16(static_cast<size_t>(total_L) * patch_dim);
  {
    size_t off = 0;
    for (const auto& pix : pixel_sets) {
      const size_t n = pix.size();
      for (size_t i = 0; i < n; ++i) {
        const __nv_bfloat16 b = __float2bfloat16_rn(pix[i]);
        pixels_bf16[off + i] = *reinterpret_cast<const uint16_t*>(&b);
      }
      off += n;
    }
  }
  pack_timing.Stop();
  // Publish no output ownership until all computation and cleanup succeeded.
  // Counts are prepared before acquiring device buffers (may throw bad_alloc).
  int total_tokens = 0;
  out_counts->reserve(shapes.size());
  for (const auto& sh : shapes) {
    const int count = (sh.h / m) * (sh.w / m) * sh.t;
    out_counts->push_back(count);
    total_tokens += count;
  }
  if (static_cast<uint64_t>(total_tokens) != plan.merged_tokens ||
      pixels_bf16.size() != plan.pixel_elements)
    return fail("media_plan_mismatch", items.size());
  if (cancelled()) return fail("request_cancelled", items.size());
  uint16_t* d_pixels = nullptr;
  uint16_t* d_out = nullptr;
  const auto record = [&](DeviceMemoryResult result) {
    if (!result.ok()) {
      timing_completed = false;
      *device_result = result;
      std::fprintf(stderr,
                   "[q4t][vision-device] id=%s code=%s error=%d pending=%d\n",
                   request_id, result.Reason(), result.operation_error,
                   result.pending_error);
      ApplyDeviceMemoryFailure(result, gpu_healthy_, serving_failed_,
                               stop_requested_);
      // Called with model_mu_ held. Never reacquire it from this path.
      if (!result.exhausted()) AbortBackendExecution();
    }
    return result.ok();
  };
  const auto release = [&](uint16_t*& pointer, const char* site) {
    TimingScope cleanup_timing(timing, TimingStage::kCleanup);
    return record(detail::ReleaseCudaMemory(pointer, site, request_id));
  };
  const int exceptions = std::uncaught_exceptions();
  ScopeExit cleanup([&] {
    if (std::uncaught_exceptions() > exceptions) AbortBackendExecution();
    release(d_pixels, "vision.pixels_free");
    release(d_out, "vision.output_free");
  });
  TimingScope allocate_timing(timing, TimingStage::kPixelsAllocate);
  if (!record(detail::AllocateCudaMemory(d_pixels,
                                         pixels_bf16.size() * sizeof(uint16_t),
                                         "vision.pixels_allocate", request_id)))
    return false;
  allocate_timing.Stop();
  TimingScope upload_timing(timing, TimingStage::kPixelsUpload);
  auto copy_error = static_cast<cudaError_t>(
      detail::DeviceMemoryFault("vision.pixels_copy", request_id));
  if (copy_error == cudaSuccess)
    copy_error = cudaMemcpy(d_pixels, pixels_bf16.data(),
                            pixels_bf16.size() * sizeof(uint16_t),
                            cudaMemcpyHostToDevice);
  if (copy_error != cudaSuccess) {
    record({DeviceMemoryState::kFailure, copy_error});
    return false;
  }
  upload_timing.Stop();
  TimingScope workspace_timing(timing, TimingStage::kWorkspace);
  DeviceMemoryResult workspace;
  if (!vision_tower_->Allocate(shapes, nullptr, &workspace)) {
    record(workspace);
    return false;
  }
  workspace_timing.Stop();
  const size_t out_bytes = vision::VisionOutputBytes(cfg, shapes);
  TimingScope output_timing(timing, TimingStage::kOutputAllocate);
  if (!record(detail::AllocateCudaMemory(d_out, out_bytes,
                                         "vision.output_allocate", request_id)))
    return false;
  output_timing.Stop();
  vision::ForwardTiming forward_events(vision::VisionForwardTimingEnabled());
  ScopeExit report_forward([&] {
    forward_events.Report(stderr, request_id, shapes.size(), plan.patches,
                           cfg.depth);
  });
  const auto timing_failure = [&] {
    const int code = forward_events.ErrorCode();
    record({DeviceMemoryState::kFailure, code ? code : cudaErrorUnknown});
  };
  if (!forward_events.Prepare(shapes.size(), cfg.depth)) {
    timing_failure();
    return false;
  }
  detail::ServeRequestFaultPoint("vision.before_forward", request_id);
  TimingScope forward_timing(timing, TimingStage::kForward);
  if (detail::DeviceMemoryFault("vision.forward", request_id) != cudaSuccess ||
      !vision::VisionForward(*vision_tower_, d_pixels, shapes, d_out, err,
                             nullptr, forward_events.Active())) {
    timing_failure();
    return false;
  }
  detail::ServeRequestFaultPoint("vision.after_forward", request_id);
  // A completion failure must not unwind or recycle possibly live buffers.
  runtime::RequireCleanupSuccess(cudaDeviceSynchronize(),
                                 "cudaDeviceSynchronize");
  forward_timing.Stop();
  if (!forward_events.CollectAfterCompletion()) {
    timing_failure();
    return false;
  }
  if (!release(d_pixels, "vision.pixels_free")) return false;
  if (cancelled()) return fail("request_cancelled", items.size());
  *out_feats = std::exchange(d_out, nullptr);
  *out_num_tokens = total_tokens;
  timing_completed = true;
  return true;
}

}  // namespace q4t::server
