#include "q4t/server/media_admission.h"

#include <cmath>

#include "../src/server/object_protocol.h"
#include "q4t/server/chat_contract.h"
#include "q4t/server/file_video.h"
#include "q4t/test.h"
#include "video_affine_fixture.h"

namespace {
using namespace q4t;
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace q4t::vision;
ProcessorConfig VideoConfig() {
  ProcessorConfig result;
  result.min_pixels = 4096;
  result.max_pixels = 25165824;
  return result;
}
constexpr const char* kPng =
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGPYoiECAAKE"
    "APFkHeRLAAAAAElFTkSuQmCC";
io::Json Part(std::string detail) {
  return Object({{"type", String("video_frames")},
                 {"detail", String(detail)},
                 {"video_frames", Array({String(kPng), String(kPng)})}});
}
}  // namespace

Q4T_TEST(video_detail_closed_native_and_prepared_contract) {
  for (const char* value : {"auto", "low", "high"}) {
    auto part = Part(value);
    ImageDetail detail;
    Q4T_CHECK(VideoInputDetail(part, &detail));
    auto request =
        Object({{"messages", Array({Object({{"role", String("user")},
                                            {"content", Array({part})}})})}});
    Q4T_CHECK(ValidateChatContract(request, "m", true));
    Set(&part, "type", String("input_video_frames"));
    Set(&part, "video_timestamps", Array({Number(0), Number(1)}));
    Set(&part, "video_frames",
        Array({String(std::string("data:image/png;base64,") + kPng),
               String(std::string("data:image/png;base64,") + kPng)}));
    io::Json mapped;
    std::string param;
    Q4T_CHECK(MapPreparedVideoContent(part, &mapped, &param) &&
              mapped.GetString("detail") == value);
  }
  for (const auto& value :
       {String("original"), String("unknown"), Number(1), io::Json{}}) {
    auto part = Part("auto");
    Set(&part, "detail", value);
    Q4T_CHECK(!VideoInputDetail(part));
  }
  return true;
}

Q4T_TEST(video_detail_high_floor_preserves_reference_auto_and_low) {
  const auto config = VideoConfig();
  MediaGeometry original, low, high, odd;
  Q4T_CHECK(PlanMediaGeometry(MediaKind::kVideo, 64, 32, 4, config, &original));
  Q4T_CHECK(PlanMediaGeometry(MediaKind::kVideo, 64, 32, 4, config, &low,
                              ImageDetail::kLow));
  Q4T_CHECK(original.width == 64 && original.height == 32 &&
            original.merged_tokens == 4 && low.width == original.width &&
            low.height == original.height &&
            low.pixel_elements == original.pixel_elements);
  Q4T_CHECK(PlanMediaGeometry(MediaKind::kVideo, 64, 32, 4, config, &high,
                              ImageDetail::kHigh));
  Q4T_CHECK(high.width == 384 && high.height == 192 &&
            high.merged_tokens == 144);
  Q4T_CHECK(PlanMediaGeometry(MediaKind::kVideo, 64, 32, 5, config, &odd,
                              ImageDetail::kHigh));
  Q4T_CHECK(odd.width == high.width && odd.height == high.height &&
            odd.padded_frames == 6 && odd.merged_tokens == 216);
  return true;
}

Q4T_TEST(video_detail_high_insufficient_budget_rejects_without_downgrade) {
  auto config = VideoConfig();
  MediaGeometry shape;
  config.max_pixels = 4 * kVideoHighMinFramePixels - 1;
  const auto result = PlanMediaGeometry(MediaKind::kVideo, 64, 32, 4, config,
                                        &shape, ImageDetail::kHigh);
  Q4T_CHECK(!result && result.error == MediaPlanError::kDetailLimit &&
            !shape.width);
  Q4T_CHECK(PlanMediaGeometry(MediaKind::kVideo, 64, 32, 4, config, &shape));
  config.max_pixels = 4 * kVideoHighMinFramePixels;
  // Aspect/alignment cannot fit the requested floor into this exact ceiling.
  Q4T_CHECK(!PlanMediaGeometry(MediaKind::kVideo, 64, 32, 4, config, &shape,
                               ImageDetail::kHigh));
  return true;
}

Q4T_TEST(video_detail_processor_matches_plan_and_preserves_quadrants) {
  FileVideoPixels decoded;
  BinaryAccess access{
      "cpu:detail",
      std::chrono::steady_clock::now() + std::chrono::seconds(15),
      {}};
  const auto source = test::video_affine::Source(test::video_affine::Scale());
  Q4T_CHECK(DecodeVideoContainer(source, {}, access, &decoded).ok());
  std::vector<const uint8_t*> frames;
  std::vector<size_t> sizes;
  for (const auto& frame : decoded.frames) {
    frames.push_back(reinterpret_cast<const uint8_t*>(frame.png.data()));
    sizes.push_back(frame.png.size());
  }
  ProcessedVideo high, automatic, low;
  std::string error;
  ProcessorOptions options;
  options.image_detail = ImageDetail::kHigh;
  Q4T_CHECK(ProcessVideo(frames, sizes, VideoConfig(), &high, &error, options));
  Q4T_CHECK(high.grid_t == 2 && high.grid_h == 12 && high.grid_w == 24);
  const int colors[][3] = {
      {255, 0, 0}, {0, 255, 0}, {0, 0, 255}, {255, 255, 0}};
  for (int group = 0; group < 2; ++group)
    for (int corner = 0; corner < 4; ++corner) {
      const int x = 384 * (corner % 2 ? 3 : 1) / 4;
      const int y = 192 * (corner / 2 ? 3 : 1) / 4;
      const int patch = group * 12 * 24 + ((y / 32) * 12 + x / 32) * 4 +
                        (y / 16 % 2) * 2 + x / 16 % 2;
      for (int c = 0; c < 3; ++c)
        for (int t = 0; t < 2; ++t) {
          const auto at =
              patch * 1536 + c * 512 + t * 256 + (y % 16) * 16 + x % 16;
          Q4T_CHECK(std::abs(high.pixel_values[at] -
                             (colors[corner][c] / 127.5f - 1)) < .08f);
        }
    }
  Q4T_CHECK(ProcessVideo(frames, sizes, VideoConfig(), &automatic, &error));
  options.image_detail = ImageDetail::kLow;
  Q4T_CHECK(ProcessVideo(frames, sizes, VideoConfig(), &low, &error, options));
  Q4T_CHECK(low.pixel_values == automatic.pixel_values && low.grid_h == 2 &&
            low.grid_w == 4);
  return true;
}

Q4T_TEST(video_detail_prompt_admission_and_capacity_are_consistent) {
  const auto request = Object(
      {{"messages", Array({Object({{"role", String("user")},
                                   {"content", Array({Part("high")})}})})}});
  std::string prompt, param, code;
  std::vector<VisionItem> items;
  Q4T_CHECK(PrepareChatPrompt(request, {}, &prompt, &items, &param, &code));
  Q4T_CHECK(items.size() == 1 && items[0].detail == ImageDetail::kHigh);
  ChatPlanConfig config;
  config.model = "m";
  config.max_len = 8192;
  config.max_prefill = 2048;
  MediaPlan plan;
  ChatPlanError error;
  const std::vector<int32_t> tokens{9, 2, 8};
  Q4T_CHECK(PlanChatMedia(items, tokens, 1, 2, {}, VideoConfig(), {48128},
                          config, &plan, &error));
  Q4T_CHECK(plan.items[0].detail == ImageDetail::kHigh &&
            plan.items[0].geometry.width == 256 &&
            plan.items[0].geometry.height == 256);
  config.max_len = 10;
  Q4T_CHECK(!PlanChatMedia(items, tokens, 1, 2, {}, VideoConfig(), {48128},
                           config, &plan, &error));
  return true;
}
