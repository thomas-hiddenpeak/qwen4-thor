#pragma once

#include <span>
#include <vector>

#include "q4t/server/session_media.h"
#include "q4t/status.h"
#include "q4t/vision/media_plan.h"

namespace q4t::server {
struct VisionItem;

struct MediaCachePlan {
  std::vector<SessionMediaBlock> blocks;
  // Raw token prefix -> expanded token prefix, including the initial zero.
  std::vector<int> token_ends;
};

Status PlanMediaCache(std::span<const uint32_t> raw,
                      std::span<const int32_t> expanded,
                      std::span<const VisionItem> items,
                      const vision::MediaPlan& admission, int image_id,
                      int video_id, const vision::ProcessorConfig& image,
                      const vision::ProcessorConfig& video,
                      MediaCachePlan* plan);

// All expanded rows from a placeholder keep the same source start. Existing
// complete-source-cluster mapping consequently cannot bisect a visual block.
Status MediaCacheOffsets(std::span<const size_t> raw_offsets,
                         const MediaCachePlan& plan,
                         std::vector<size_t>* expanded_offsets);

std::string MediaCacheKeySuffix(std::span<const SessionMediaBlock> media,
                                int prefix);

}  // namespace q4t::server
