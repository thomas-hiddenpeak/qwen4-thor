#include "q4t/vision/media_plan.h"

#include <filesystem>
#include <fstream>
#include <limits>

#include "../src/server/object_protocol.h"
#include "q4t/server/chat_contract.h"
#include "q4t/server/file_input.h"
#include "q4t/server/media_admission.h"
#include "q4t/server/responses_input.h"
#include "q4t/test.h"

namespace {
using namespace q4t::vision;
using namespace q4t::server;
using namespace q4t::server::object_detail;
using q4t::io::Json;
std::string Read(const char* name) {
  std::ifstream file(std::filesystem::path(__FILE__).parent_path() /
                         "fixtures/image_detail" / name,
                     std::ios::binary);
  if (!file) throw std::runtime_error("image detail fixture missing");
  return std::string(std::istreambuf_iterator<char>(file), {});
}
Json Parse(const std::string& text) {
  Json result;
  if (!q4t::io::ParseJson(text, &result, true))
    throw std::runtime_error("image detail JSON");
  return result;
}
constexpr std::pair<const char*, ImageDetail> kModes[] = {
    {"auto", ImageDetail::kAuto},
    {"low", ImageDetail::kLow},
    {"high", ImageDetail::kHigh},
    {"original", ImageDetail::kOriginal}};
constexpr const char* kPng =
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGPYoiECAAKE"
    "APFkHeRLAAAAAElFTkSuQmCC";
}  // namespace

Q4T_TEST(image_detail_geometry_preserves_auto_and_enforces_native_bounds) {
  ProcessorConfig config;
  MediaGeometry geometry;
  // Existing Python round-to-even edge and temporal contract are unchanged.
  Q4T_CHECK(
      PlanMediaGeometry(MediaKind::kImage, 272, 272, 1, config, &geometry));
  Q4T_CHECK(geometry.width == 256 && geometry.height == 256);
  for (auto mode : {ImageDetail::kLow, ImageDetail::kHigh}) {
    const int edge = mode == ImageDetail::kLow ? 512 : 2048;
    for (auto [width, height] :
         {std::pair{4096u, 2048u}, {17u, 9u}, {6400u, 32u}, {32u, 6400u}}) {
      Q4T_CHECK(PlanMediaGeometry(MediaKind::kImage, width, height, 1, config,
                                  &geometry, mode));
      Q4T_CHECK(geometry.width <= edge && geometry.height <= edge);
      Q4T_CHECK(geometry.width % 32 == 0 && geometry.height % 32 == 0);
    }
    config.min_pixels = 1024;
    config.max_pixels = 65536;
    Q4T_CHECK(PlanMediaGeometry(MediaKind::kImage, 4096, 2048, 1, config,
                                &geometry, mode));
    Q4T_CHECK(geometry.width * geometry.height <= config.max_pixels);
    config = {};
  }
  Q4T_CHECK(PlanMediaGeometry(MediaKind::kImage, 17, 9, 1, config, &geometry,
                              ImageDetail::kOriginal));
  Q4T_CHECK(geometry.width == 32 && geometry.height == 32);
  config.min_pixels = config.max_pixels = 2048;
  auto result = PlanMediaGeometry(MediaKind::kImage, 65, 33, 1, config,
                                  &geometry, ImageDetail::kOriginal);
  Q4T_CHECK(!result && result.error == MediaPlanError::kDetailLimit);
  Q4T_CHECK(geometry.pixel_elements == 0 && geometry.width == 0);
  Q4T_CHECK(!PlanMediaGeometry(MediaKind::kVideo, 32, 32, 2, config, &geometry,
                               ImageDetail::kHigh));
  Q4T_CHECK(!PlanMediaGeometry(MediaKind::kImage, 32, 32, 1, config, &geometry,
                               static_cast<ImageDetail>(99)));
  Q4T_CHECK(!PlanMediaGeometry(MediaKind::kImage,
                               std::numeric_limits<uint32_t>::max(), 32, 1,
                               config, &geometry, ImageDetail::kOriginal));
  return true;
}

Q4T_TEST(image_detail_actual_pixels_match_independent_pillow_four_modes) {
  const auto bytes = Read("pattern.png");
  const auto reference = Parse(Read("reference.json"));
  Q4T_CHECK(Digest(bytes) == reference.GetString("source_sha256"));
  for (const auto& [name, mode] : kModes) {
    const auto& expected = *reference.Find("modes")->Find(name);
    const MediaBytes frame{reinterpret_cast<const uint8_t*>(bytes.data()),
                           bytes.size()};
    const MediaPlanInput input{MediaKind::kImage, {&frame, 1}, mode};
    MediaPlan plan;
    Q4T_CHECK(PlanMediaSources({&input, 1}, {}, {}, {}, &plan));
    ProcessorOptions options;
    options.image_detail = mode;
    options.admission = &plan.items[0];
    ProcessedImage output;
    std::string error;
    Q4T_CHECK(
        ProcessImage(frame.data(), frame.size(), {}, &output, &error, options));
    Q4T_CHECK(output.grid_w * 16 == expected.GetInt("width") &&
              output.grid_h * 16 == expected.GetInt("height"));
    Q4T_CHECK(output.pixel_values.size() ==
              static_cast<size_t>(expected.GetInt("elements")));
    const std::string_view raw(
        reinterpret_cast<const char*>(output.pixel_values.data()),
        output.pixel_values.size() * sizeof(float));
    const auto actual = Digest(raw);
    if (actual != expected.GetString("sha256"))
      std::fprintf(stderr, "detail=%s actual=%s\n", name, actual.c_str());
    Q4T_CHECK(actual == expected.GetString("sha256"));
    Q4T_CHECK(plan.pixel_elements == output.pixel_values.size() &&
              plan.merged_tokens == static_cast<uint64_t>(output.L() / 4));
  }
  return true;
}

Q4T_TEST(image_detail_original_small_pixels_and_admission_mismatch) {
  std::string bytes;
  const char* code;
  Q4T_CHECK(DecodeMediaUrl(kPng, &bytes, &code));
  MediaBytes frame{reinterpret_cast<const uint8_t*>(bytes.data()),
                   bytes.size()};
  MediaPlanInput input{MediaKind::kImage, {&frame, 1}, ImageDetail::kOriginal};
  MediaPlan plan;
  Q4T_CHECK(PlanMediaSources({&input, 1}, {}, {}, {}, &plan));
  Q4T_CHECK(plan.merged_tokens == 1);
  ProcessorOptions options;
  options.image_detail = ImageDetail::kOriginal;
  options.admission = &plan.items[0];
  ProcessedImage output, automatic;
  std::string error;
  Q4T_CHECK(
      ProcessImage(frame.data(), frame.size(), {}, &output, &error, options));
  Q4T_CHECK(output.grid_w == 2 && output.grid_h == 2);
  Q4T_CHECK(ProcessImage(frame.data(), frame.size(), {}, &automatic, &error));
  Q4T_CHECK(automatic.grid_w == 16 && automatic.grid_h == 16);
  // The 1x1 source is constant. Padding must preserve each exact channel.
  for (size_t patch = 0; patch < 4; ++patch)
    for (size_t channel = 0; channel < 3; ++channel)
      for (size_t i = 0; i < 512; ++i)
        Q4T_CHECK(output.pixel_values[patch * 1536 + channel * 512 + i] ==
                  automatic.pixel_values[channel * 512]);
  // Same geometry is insufficient: a changed policy invalidates admission.
  ProcessorConfig small;
  small.min_pixels = small.max_pixels = 1024;
  options.image_detail = ImageDetail::kAuto;
  Q4T_CHECK(!ProcessImage(frame.data(), frame.size(), small, &output, &error,
                          options));
  Q4T_CHECK(error == "media_plan_mismatch" && output.pixel_values.empty());
  options.image_detail = ImageDetail::kOriginal;
  options.cancelled = [](void*) { return true; };
  Q4T_CHECK(
      !ProcessImage(frame.data(), frame.size(), {}, &output, &error, options));
  Q4T_CHECK(error == "request_cancelled" && output.pixel_values.empty());
  return true;
}

Q4T_TEST(image_detail_chat_responses_and_source_error_paths_agree) {
  for (const auto& [name, mode] : kModes) {
    auto request = Object(
        {{"input",
          Array({Object(
              {{"role", String("user")},
               {"content", Array({Object({{"type", String("input_image")},
                                          {"image_url", String(kPng)},
                                          {"detail", String(name)}})})}})})}});
    ResponsesInputPlan response;
    ChatPlanError plan_error;
    Q4T_CHECK(NormalizeResponsesInput(request, &response, &plan_error));
    const auto chat = Object({{"messages", response.messages}});
    std::string error, prompt;
    Q4T_CHECK(ValidateChatContract(chat, "m", true, &error));
    std::vector<VisionItem> items;
    Q4T_CHECK(PrepareChatPrompt(chat, {}, &prompt, &items, &error));
    Q4T_CHECK(items.size() == 1 && items[0].detail == mode);
    const auto& part = response.messages.array[0].Find("content")->array[0];
    FileInputSource source;
    Q4T_CHECK(ParseImageInputSource(part, FileInputProfile::kChat,
                                    "messages[0].content[0]", &source)
                  .ok());
    Q4T_CHECK(source.detail == name);
  }
  const auto bad = Parse(R"({"type":"image_url","image_url":
    {"url":"x","detail":"bad"}})");
  FileInputSource source;
  auto reply = ParseImageInputSource(bad, FileInputProfile::kChat,
                                     "messages[0].content[0]", &source);
  Q4T_CHECK(reply.http_status == 400 &&
            reply.value.GetString("param") ==
                "messages[0].content[0].image_url.detail");
  std::string error;
  const auto chat =
      Object({{"messages", Array({Object({{"role", String("user")},
                                          {"content", Array({bad})}})})}});
  Q4T_CHECK(!ValidateChatContract(chat, "m", true, &error));
  Q4T_CHECK(error == reply.value.GetString("param"));
  std::string prompt, code;
  std::vector<VisionItem> items;
  Q4T_CHECK(!PrepareChatPrompt(chat, {}, &prompt, &items, &error, &code));
  Q4T_CHECK(code == "media_detail_invalid" &&
            error == reply.value.GetString("param") && items.empty());
  return true;
}

Q4T_TEST(image_detail_count_admission_and_generation_share_geometry) {
  const auto bytes = Read("pattern.png");
  const auto reference = Parse(Read("reference.json"));
  for (const auto& [name, mode] : kModes) {
    const auto& expected = *reference.Find("modes")->Find(name);
    std::vector<VisionItem> items{{VisionItem::kImage, {bytes}, 0, 0, mode}};
    const std::vector<int32_t> tokens{9, 1, 8};
    ChatPlanConfig config;
    config.max_prefill = 8192;
    config.max_len = 16384;
    const MediaDeviceBudget device{48128};
    MediaPlan generation, count;
    ChatPlanError error;
    Q4T_CHECK(PlanChatMedia(items, tokens, 1, 2, {}, {}, device, config,
                            &generation, &error));
    config.count_input_only = true;
    Q4T_CHECK(PlanChatMedia(items, tokens, 1, 2, {}, {}, device, config, &count,
                            &error));
    Q4T_CHECK(count.expanded_prompt_tokens ==
              generation.expanded_prompt_tokens);
    const uint64_t expected_tokens =
        expected.GetInt("width") * expected.GetInt("height") / 1024;
    Q4T_CHECK(count.merged_tokens == expected_tokens &&
              count.expanded_prompt_tokens == expected_tokens + 2);
    config.max_len = static_cast<int>(expected_tokens + 2);
    Q4T_CHECK(!PlanChatMedia(items, tokens, 1, 2, {}, {}, device, config,
                             &count, &error));
    Q4T_CHECK(error.code == "context_length_exceeded" && count.item_count == 0);
  }
  return true;
}
