#pragma once

#include <span>
#include <string>
#include <string_view>

#include "q4t/server/chat_plan.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/vision/media_plan.h"

namespace q4t::server {

// Canonical standard base64, optionally in an exact PNG/JPEG data URL.
// Validates the entire encoded alphabet/padding before allocating the output.
Status DecodeMediaUrl(std::string_view url, std::string* bytes,
                      const char** code);
std::string MediaInputParam(const VisionItem& item, size_t frame = 0);
Status PlanChatMedia(std::span<const VisionItem> items,
                     std::span<const int32_t> tokens, int image_id,
                     int video_id, const vision::ProcessorConfig& image,
                     const vision::ProcessorConfig& video,
                     const vision::MediaDeviceBudget& device,
                     const ChatPlanConfig& config, vision::MediaPlan* plan,
                     ChatPlanError* error);
std::string MediaLimitsJson(uint64_t attention_shared_bytes);

}  // namespace q4t::server
