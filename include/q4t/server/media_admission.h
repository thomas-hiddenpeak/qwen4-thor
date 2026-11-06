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
// One timestamp per supplied frame, in seconds. Missing timestamps use the
// fixed reference processor's 24 fps fallback. No sampling occurs here.
Status VideoFrameTimes(const io::Json& part, size_t frames,
                       std::vector<double>* times, std::string* error_field);
std::string MediaInputParam(const VisionItem& item, size_t frame = 0);
Status PlanChatMedia(std::span<const VisionItem> items,
                     std::span<const int32_t> tokens, int image_id,
                     int video_id, const vision::ProcessorConfig& image,
                     const vision::ProcessorConfig& video,
                     const vision::MediaDeviceBudget& device,
                     const ChatPlanConfig& config, vision::MediaPlan* plan,
                     ChatPlanError* error);
// Shared preflight/generation planning. No pixel decode or device allocation.
// Token/budget outputs are published only after all expansion checks succeed.
Status ExpandChatMedia(const io::Json& request,
                       std::span<const VisionItem> items, int image_id,
                       int video_id, const vision::ProcessorConfig& image,
                       const vision::ProcessorConfig& video,
                       const vision::MediaDeviceBudget& device,
                       const ChatPlanConfig& config,
                       std::vector<uint32_t>* tokens, vision::MediaPlan* media,
                       ChatPromptBudget* budget, ChatPlanError* error);
std::string MediaLimitsJson(uint64_t attention_shared_bytes);

}  // namespace q4t::server
