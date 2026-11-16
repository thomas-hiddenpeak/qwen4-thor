#include "q4t/server/media_cache.h"

#include <bit>
#include <limits>

#include "q4t/server/chat_prompt.h"

namespace q4t::server {
namespace {
trace::Digest Identity(const VisionItem& item,
                       const vision::MediaItemPlan& admission,
                       const vision::ProcessorConfig& processor, size_t group) {
  // Do not hash object padding, addresses, transport URLs or credentials.
  std::string value = "q4t-media-state-v1";
  const auto number = [&](int64_t n) { value += ':' + std::to_string(n); };
  number(item.kind);
  number(static_cast<int>(item.detail));
  number(admission.source_width);
  number(admission.source_height);
  number(admission.geometry.width);
  number(admission.geometry.height);
  number(admission.geometry.grid_h);
  number(admission.geometry.grid_w);
  number(processor.patch_size);
  number(processor.temporal_patch_size);
  number(processor.merge_size);
  number(processor.min_pixels);
  number(processor.max_pixels);
  for (const auto f : processor.image_mean) number(std::bit_cast<uint32_t>(f));
  for (const auto f : processor.image_std) number(std::bit_cast<uint32_t>(f));
  const size_t first = item.kind == VisionItem::kImage ? 0 : group * 2;
  number(group);
  number(std::min(size_t{2}, item.frames.size() - first));
  for (size_t i = 0; i < 2; ++i) {
    const auto& frame =
        item.frames[std::min(first + i, item.frames.size() - 1)];
    value +=
        ':' +
        trace::HexDigest(trace::Sha256(
            {reinterpret_cast<const uint8_t*>(frame.data()), frame.size()}));
  }
  return trace::Sha256(
      {reinterpret_cast<const uint8_t*>(value.data()), value.size()});
}
}  // namespace

Status PlanMediaCache(std::span<const uint32_t> raw,
                      std::span<const int32_t> expanded,
                      std::span<const VisionItem> items,
                      const vision::MediaPlan& admission, int image_id,
                      int video_id, const vision::ProcessorConfig& image,
                      const vision::ProcessorConfig& video,
                      MediaCachePlan* plan) {
  *plan = {};
  const auto invalid = [] {
    return Status::Fail("media cache identity mismatch");
  };
  if (items.size() != admission.item_count || items.empty() ||
      items.size() > vision::kMediaPlanItems || image_id <= 0 ||
      video_id <= 0 || image_id == video_id || raw.empty() ||
      expanded.size() != admission.expanded_prompt_tokens ||
      expanded.size() > static_cast<size_t>(std::numeric_limits<int>::max()))
    return invalid();
  MediaCachePlan result;
  result.token_ends.push_back(0);
  size_t item = 0, group = 0, at = 0;
  for (const auto token : raw) {
    if (token > static_cast<uint32_t>(std::numeric_limits<int32_t>::max()) ||
        at >= expanded.size())
      return invalid();
    size_t count = 1;
    if (token == static_cast<uint32_t>(image_id) ||
        token == static_cast<uint32_t>(video_id)) {
      if (item >= items.size()) return invalid();
      const auto& source = items[item];
      const auto& input = admission.items[item];
      const auto& geometry = input.geometry;
      const bool is_image = source.kind == VisionItem::kImage;
      if ((source.kind != VisionItem::kImage &&
           source.kind != VisionItem::kVideo) ||
          token != static_cast<uint32_t>(is_image ? image_id : video_id) ||
          source.frames.empty() || source.frames.size() != input.frames ||
          source.frames.size() > vision::kMediaPlanFrames ||
          geometry.grid_t !=
              static_cast<int>(is_image ? 1 : (source.frames.size() + 1) / 2) ||
          !geometry.merged_tokens || geometry.merged_tokens % geometry.grid_t ||
          geometry.merged_tokens / geometry.grid_t > expanded.size() - at ||
          source.detail != input.detail ||
          input.kind != (is_image ? vision::MediaKind::kImage
                                  : vision::MediaKind::kVideo))
        return invalid();
      count = geometry.merged_tokens / geometry.grid_t;
      result.blocks.push_back(
          {static_cast<int>(at), static_cast<int>(at + count),
           Identity(source, input, is_image ? image : video, group)});
      if (++group == static_cast<size_t>(geometry.grid_t)) {
        ++item;
        group = 0;
      }
    }
    for (size_t i = 0; i < count; ++i)
      if (expanded[at + i] != static_cast<int32_t>(token)) return invalid();
    at += count;
    result.token_ends.push_back(static_cast<int>(at));
  }
  if (at != expanded.size() || item != items.size() || group ||
      SessionMediaPrefix(result.blocks, static_cast<int>(at)) < 0)
    return invalid();
  *plan = std::move(result);
  return {};
}

Status MediaCacheOffsets(std::span<const size_t> raw_offsets,
                         const MediaCachePlan& plan,
                         std::vector<size_t>* expanded_offsets) {
  expanded_offsets->clear();
  if (plan.token_ends.size() != raw_offsets.size() + 1 ||
      plan.token_ends.empty() || plan.token_ends[0] != 0 ||
      plan.token_ends.back() < 0 || plan.token_ends.back() > 262144 ||
      !std::is_sorted(raw_offsets.begin(), raw_offsets.end()))
    return Status::Fail("media cache source offset mismatch");
  for (size_t i = 1; i < plan.token_ends.size(); ++i)
    if (plan.token_ends[i] <= plan.token_ends[i - 1])
      return Status::Fail("media cache expansion is not increasing");
  expanded_offsets->reserve(plan.token_ends.back());
  for (size_t i = 0; i < raw_offsets.size(); ++i)
    expanded_offsets->insert(expanded_offsets->end(),
                             plan.token_ends[i + 1] - plan.token_ends[i],
                             raw_offsets[i]);
  return {};
}

std::string MediaCacheKeySuffix(std::span<const SessionMediaBlock> media,
                                int prefix) {
  const int count = SessionMediaPrefix(media, prefix);
  if (count <= 0) return {};
  std::string value = "media-v1";
  for (int i = 0; i < count; ++i) {
    const auto& block = media[i];
    value += ':' + std::to_string(block.begin) + ':' +
             std::to_string(block.end) + ':' + trace::HexDigest(block.identity);
  }
  return ':' +
         trace::HexDigest(trace::Sha256(
             {reinterpret_cast<const uint8_t*>(value.data()), value.size()}));
}

}  // namespace q4t::server
