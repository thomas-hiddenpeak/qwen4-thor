#include "q4t/server/media_admission.h"

#include <array>

namespace q4t::server {
namespace {
int Digit(unsigned char c) {
  if (c >= 'A' && c <= 'Z') return c - 'A';
  if (c >= 'a' && c <= 'z') return c - 'a' + 26;
  if (c >= '0' && c <= '9') return c - '0' + 52;
  if (c == '+') return 62;
  if (c == '/') return 63;
  return -1;
}
}  // namespace

Status DecodeMediaUrl(std::string_view url, std::string* bytes,
                      const char** code) {
  bytes->clear();
  *code = "ok";
  const auto fail = [&](const char* reason) {
    bytes->clear();
    *code = reason;
    return Status::Fail(reason);
  };
  int format = -1;
  constexpr std::string_view png = "data:image/png;base64,";
  constexpr std::string_view jpeg = "data:image/jpeg;base64,";
  if (url.starts_with(png)) {
    url.remove_prefix(png.size());
    format = 0;
  } else if (url.starts_with(jpeg)) {
    url.remove_prefix(jpeg.size());
    format = 1;
  } else if (url.starts_with("data:") || url.starts_with("http://") ||
             url.starts_with("https://"))
    return fail("media_url_unsupported");
  if (url.empty() || url.size() % 4) return fail("media_base64_invalid");
  size_t padding = url.back() == '=' ? 1 : 0;
  if (padding && url[url.size() - 2] == '=') ++padding;
  const size_t digits = url.size() - padding;
  for (size_t i = 0; i < digits; ++i)
    if (Digit(url[i]) < 0) return fail("media_base64_invalid");
  if ((padding == 2 && (Digit(url[digits - 1]) & 15)) ||
      (padding == 1 && (Digit(url[digits - 1]) & 3)))
    return fail("media_base64_invalid");
  const size_t decoded = url.size() / 4 * 3 - padding;
  if (!decoded || decoded > vision::MediaPlanLimits{}.max_encoded_bytes)
    return fail("media_encoded_limit");
  bytes->resize(decoded);
  uint32_t value = 0;
  int bits = -8;
  size_t output = 0;
  for (size_t i = 0; i < digits; ++i) {
    value = (value << 6) | static_cast<uint32_t>(Digit(url[i]));
    bits += 6;
    if (bits >= 0) {
      (*bytes)[output++] = static_cast<char>((value >> bits) & 255);
      bits -= 8;
    }
  }
  vision::MediaHeader header;
  const auto checked = vision::ReadMediaHeader(
      {reinterpret_cast<const uint8_t*>(bytes->data()), bytes->size()},
      &header);
  if (!checked) return fail(vision::MediaPlanErrorCode(checked.error));
  if ((format == 0 && header.format != vision::MediaFormat::kPng) ||
      (format == 1 && header.format != vision::MediaFormat::kJpeg))
    return fail("media_format_mismatch");
  return Status();
}

std::string MediaInputParam(const VisionItem& item, size_t frame) {
  auto param = "messages[" + std::to_string(item.message_index) + "].content[" +
               std::to_string(item.part_index) + "]";
  if (item.kind == VisionItem::kImage) return param + ".image_url";
  return param + ".video_frames[" + std::to_string(frame) + "]";
}

Status PlanChatMedia(std::span<const VisionItem> items,
                     std::span<const int32_t> tokens, int image_id,
                     int video_id, const vision::ProcessorConfig& image,
                     const vision::ProcessorConfig& video,
                     const vision::MediaDeviceBudget& device,
                     const ChatPlanConfig& config, vision::MediaPlan* plan,
                     ChatPlanError* error) {
  *plan = {};
  *error = {};
  const auto fail = [&](const char* code, size_t item = 0, size_t frame = 0) {
    error->code = code;
    error->param =
        item < items.size() ? MediaInputParam(items[item], frame) : "messages";
    return Status::Fail(code);
  };
  if (items.empty() || items.size() > vision::kMediaPlanItems)
    return fail("media_item_limit", items.size());
  if (image_id <= 0 || video_id <= 0 || image_id == video_id)
    return fail("media_config_invalid", items.size());
  std::array<vision::MediaPlanInput, vision::kMediaPlanItems> inputs;
  std::array<std::array<vision::MediaBytes, vision::kMediaPlanFrames>,
             vision::kMediaPlanItems>
      frames;
  for (size_t i = 0; i < items.size(); ++i) {
    const auto& item = items[i];
    if (item.kind != VisionItem::kImage && item.kind != VisionItem::kVideo)
      return fail("media_config_invalid", i);
    if (item.frames.size() > vision::kMediaPlanFrames)
      return fail("media_frame_limit", i);
    for (size_t f = 0; f < item.frames.size(); ++f)
      frames[i][f] = {reinterpret_cast<const uint8_t*>(item.frames[f].data()),
                      item.frames[f].size()};
    inputs[i] = {item.kind == VisionItem::kImage ? vision::MediaKind::kImage
                                                 : vision::MediaKind::kVideo,
                 {frames[i].data(), item.frames.size()}};
  }
  size_t placeholders = 0;
  for (const auto token : tokens) {
    if (token != image_id && token != video_id) continue;
    if (placeholders >= items.size() ||
        (items[placeholders].kind == VisionItem::kImage ? image_id
                                                        : video_id) != token)
      return fail("media_placeholder_count", items.size());
    ++placeholders;
  }
  const vision::MediaContextBudget context{
      tokens.size(), placeholders, static_cast<uint64_t>(config.max_prefill),
      static_cast<uint64_t>(config.max_len)};
  const auto checked = vision::PlanMedia({inputs.data(), items.size()}, image,
                                         video, {}, context, device, plan);
  if (!checked) {
    const bool prompt =
        checked.error == vision::MediaPlanError::kContextLength ||
        checked.error == vision::MediaPlanError::kPrefillLength ||
        checked.error == vision::MediaPlanError::kPlaceholderCount;
    return fail(vision::MediaPlanErrorCode(checked.error),
                prompt ? items.size() : checked.item, checked.frame);
  }
  return Status();
}

std::string MediaLimitsJson(uint64_t attention_shared_bytes) {
  const vision::MediaPlanLimits limits;
  return R"({"profile":"q4t-media-admission-v1","experimental":true,)"
         R"("roles":["user"],"formats":["png","jpeg"],)"
         R"("data_url_mime":["image/png","image/jpeg"],)"
         R"("raw_base64":true,"base64":"canonical_standard_padded",)"
         R"("image_detail":["auto"],"remote_urls":false,)"
         R"("max_items":)" +
         std::to_string(limits.max_items) + R"(,"max_frames_per_item":)" +
         std::to_string(limits.max_frames_per_item) +
         R"(,"max_encoded_bytes":)" + std::to_string(limits.max_encoded_bytes) +
         R"(,"max_source_edge":)" + std::to_string(limits.max_source_edge) +
         R"(,"max_source_pixels":)" + std::to_string(limits.max_source_pixels) +
         R"(,"max_decoded_rgb_bytes":)" +
         std::to_string(limits.max_decoded_rgb_bytes) + R"(,"max_patches":)" +
         std::to_string(limits.max_patches) + R"(,"max_workspace_bytes":)" +
         std::to_string(limits.max_workspace_bytes) +
         R"(,"decoder_owned_bytes":)" +
         std::to_string(vision::kDecoderOwnedBytes) +
         R"(,"attention_shared_bytes":)" +
         (attention_shared_bytes ? std::to_string(attention_shared_bytes)
                                 : "null") +
         R"(,"unknown_device_limit":"reject","whole_server_ram_limit":false,)"
         R"("cancellation":"item_frame_and_before_gpu_not_preemptive"})";
}

}  // namespace q4t::server
