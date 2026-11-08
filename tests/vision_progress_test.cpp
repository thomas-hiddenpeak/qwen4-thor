// Actual CPU image/video paths; no CUDA, model assets or image library added.
#include "q4t/runtime/execution_progress.h"
#include "q4t/server/progress_context.h"
#include "q4t/test.h"
#include "q4t/vision/processor.h"

#include <cstdint>
#include <string>
#include <vector>

namespace {
using namespace q4t::server;
using namespace q4t::vision;
using q4t::runtime::ExecutionActivityKind;
using q4t::runtime::ExecutionProgressEvent;
// Generated 32x32 RGB PNG, constant (23,71,149), zlib + checked PNG chunks.
const std::vector<uint8_t> kImage = {
    0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d,
    0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x20, 0x00, 0x00, 0x00, 0x20,
    0x08, 0x02, 0x00, 0x00, 0x00, 0xfc, 0x18, 0xed, 0xa3, 0x00, 0x00, 0x00,
    0x2a, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9c, 0x63, 0x10, 0x77, 0x9f, 0x4a,
    0x53, 0xc4, 0x30, 0x6a, 0xc1, 0xa8, 0x05, 0xa3, 0x16, 0x8c, 0x5a, 0x30,
    0x6a, 0xc1, 0xa8, 0x05, 0xa3, 0x16, 0x8c, 0x5a, 0x30, 0x6a, 0xc1, 0xa8,
    0x05, 0xa3, 0x16, 0x0c, 0x15, 0x0b, 0x00, 0x2d, 0x5c, 0xcc, 0x2e, 0xb2,
    0x7f, 0xf2, 0xd6, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e, 0x44, 0xae,
    0x42, 0x60, 0x82,
};
ProcessorConfig Config() {
  ProcessorConfig value;
  value.min_pixels = value.max_pixels = 1024;
  return value;
}
struct Capture {
  const RequestProgress& progress;
  q4t::runtime::ExecutionProgressObserver previous;
  uint64_t seen = 0;
  bool coherent = true;
  explicit Capture(const RequestProgress& value)
      : progress(value), previous(q4t::runtime::execution_progress_observer) {
    q4t::runtime::execution_progress_observer = {this, Observe};
  }
  ~Capture() { q4t::runtime::execution_progress_observer = previous; }
  static void Observe(void* context, ExecutionActivityKind activity,
                      ExecutionProgressEvent event) noexcept {
    auto& self = *static_cast<Capture*>(context);
    if (self.previous.notify)
      self.previous.notify(self.previous.context, activity, event);
    if (event != ExecutionProgressEvent::kCompleted) return;
    const auto value = self.progress.Read();
    self.coherent = self.coherent && value.complete && value.count == 1;
    if (value.count == 1)
      self.seen |= uint64_t{1} << uint64_t(value.rows[0].stage);
  }
};
constexpr uint64_t kCpuStages =
    (uint64_t{1} << uint64_t(ProgressStage::kMediaDecode)) |
    (uint64_t{1} << uint64_t(ProgressStage::kMediaResize)) |
    (uint64_t{1} << uint64_t(ProgressStage::kMediaNormalize)) |
    (uint64_t{1} << uint64_t(ProgressStage::kMediaPatchify));
}  // namespace

Q4T_TEST(progress_actual_image_stages_preserve_pixels) {
  ProcessedImage baseline, observed;
  std::string error;
  Q4T_CHECK(ProcessImage(kImage.data(), kImage.size(), Config(),
                          &baseline, &error));
  RequestProgress progress;
  auto lease = progress.Acquire(ProgressNowNs() + 1000000000);
  lease.Handle().Stage(ProgressStage::kMedia);
  ProgressContext context(lease.Handle());
  ProgressExecution work(ProgressWork::kMedia);
  Capture capture(progress);
  Q4T_CHECK(ProcessImage(kImage.data(), kImage.size(), Config(),
                          &observed, &error));
  Q4T_CHECK(observed.pixel_values == baseline.pixel_values &&
            observed.grid_h == baseline.grid_h &&
            observed.grid_w == baseline.grid_w);
  const auto value = progress.Read();
  Q4T_CHECK(capture.coherent && capture.seen == kCpuStages && value.complete &&
            value.rows[0].stage == ProgressStage::kMedia &&
            value.rows[0].work_id == 1 && value.rows[0].completed == 4);
  return true;
}

Q4T_TEST(progress_actual_video_frame_completion_preserves_pixels) {
  const std::vector<const uint8_t*> frames(3, kImage.data());
  const std::vector<size_t> sizes(3, kImage.size());
  ProcessedVideo baseline, observed;
  std::string error;
  Q4T_CHECK(ProcessVideo(frames, sizes, Config(), &baseline, &error));
  RequestProgress progress;
  auto lease = progress.Acquire(ProgressNowNs() + 1000000000);
  lease.Handle().Stage(ProgressStage::kMedia);
  ProgressContext context(lease.Handle());
  ProgressExecution work(ProgressWork::kMedia);
  Capture capture(progress);
  Q4T_CHECK(ProcessVideo(frames, sizes, Config(), &observed, &error));
  Q4T_CHECK(observed.pixel_values == baseline.pixel_values &&
            observed.grid_t == baseline.grid_t && observed.grid_t == 2);
  const auto value = progress.Read();
  Q4T_CHECK(capture.coherent && capture.seen == kCpuStages && value.complete &&
            value.rows[0].stage == ProgressStage::kMedia &&
            value.rows[0].completed ==
                11);  // 3 decode + 3 resize + 4 norm + patch.
  return true;
}

Q4T_TEST(progress_actual_media_failure_and_cancellation_do_not_complete) {
  RequestProgress progress;
  auto lease = progress.Acquire(ProgressNowNs() + 1000000000);
  lease.Handle().Stage(ProgressStage::kMedia);
  ProgressContext context(lease.Handle());
  ProgressExecution work(ProgressWork::kMedia);
  ProcessedImage output;
  std::string error;
  Q4T_CHECK(!ProcessImage(kImage.data(), 40, Config(), &output, &error));
  ProcessorOptions options;
  options.cancelled = [](void*) { return true; };
  Q4T_CHECK(!ProcessImage(kImage.data(), kImage.size(), Config(), &output,
                          &error, options));
  const auto value = progress.Read();
  Q4T_CHECK(value.complete && value.rows[0].completed == 0 &&
            value.rows[0].stage == ProgressStage::kMedia);
  Q4T_CHECK(output.pixel_values.empty());
  return true;
}
