#include "q4t/server/media_cache.h"

#include <future>
#include <stdexcept>

#include "q4t/model/vision_positions.h"
#include "q4t/runtime/prefill_kernels.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/prompt_cache_policy.h"
#include "q4t/test.h"

namespace {
using namespace q4t;
using namespace q4t::server;

struct Fixture {
  vision::ProcessorConfig image, video;
  std::vector<uint32_t> raw{7, 1, 8, 2, 9, 2, 10};
  std::vector<int32_t> expanded{7, 1, 1, 1, 1, 1, 1, 8, 2,
                                2, 2, 2, 9, 2, 2, 2, 2, 10};
  std::vector<VisionItem> items{
      {VisionItem::kImage, {"image pixels"}},
      {VisionItem::kVideo, {"frame 0", "frame 1", "frame 2"}}};
  vision::MediaPlan admission;
  Fixture() {
    admission.item_count = 2;
    admission.expanded_prompt_tokens = expanded.size();
    auto& a = admission.items[0];
    a.frames = 1;
    a.source_width = a.geometry.width = 96;
    a.source_height = a.geometry.height = 64;
    a.geometry.grid_t = 1;
    a.geometry.grid_h = 4;
    a.geometry.grid_w = 6;
    a.geometry.merged_tokens = 6;
    auto& b = admission.items[1];
    b.kind = vision::MediaKind::kVideo;
    b.frames = 3;
    b.source_width = b.geometry.width = 64;
    b.source_height = b.geometry.height = 64;
    b.geometry.grid_t = 2;
    b.geometry.grid_h = b.geometry.grid_w = 4;
    b.geometry.merged_tokens = 8;
  }
  Status Plan(MediaCachePlan* out) {
    return PlanMediaCache(raw, expanded, items, admission, 1, 2, image, video,
                          out);
  }
};
}  // namespace

Q4T_TEST(vision_prefill_kernel_scope_restores_decode_on_return_and_exception) {
  Q4T_CHECK(!runtime::UsePrefillKernels());
  {
    runtime::PrefillKernelScope prefill(true);
    Q4T_CHECK(runtime::UsePrefillKernels());
    try {
      runtime::PrefillKernelScope decode(false);
      Q4T_CHECK(!runtime::UsePrefillKernels());
      throw std::runtime_error("cancelled prefill");
    } catch (const std::runtime_error&) {
      Q4T_CHECK(runtime::UsePrefillKernels());
    }
  }
  Q4T_CHECK(!runtime::UsePrefillKernels());
  return true;
}

Q4T_TEST(vision_prefill_kernel_scope_does_not_change_another_worker) {
  runtime::PrefillKernelScope prefill(true);
  auto worker = std::async(std::launch::async, [] {
    if (runtime::UsePrefillKernels()) return false;
    {
      runtime::PrefillKernelScope inner(true);
      if (!runtime::UsePrefillKernels()) return false;
    }
    return !runtime::UsePrefillKernels();
  });
  Q4T_CHECK(worker.get());
  Q4T_CHECK(runtime::UsePrefillKernels());
  return true;
}

Q4T_TEST(media_cache_identity_and_expanded_boundaries) {
  Fixture f;
  MediaCachePlan a, b;
  Q4T_CHECK(f.Plan(&a) && f.Plan(&b));
  Q4T_CHECK(a.blocks == b.blocks && a.blocks.size() == 3);
  Q4T_CHECK(a.token_ends == std::vector<int>({0, 1, 7, 8, 12, 13, 17, 18}));
  Q4T_CHECK(a.blocks[0].begin == 1 && a.blocks[0].end == 7 &&
            a.blocks[1].begin == 8 && a.blocks[1].end == 12 &&
            a.blocks[2].begin == 13 && a.blocks[2].end == 17);
  return true;
}

Q4T_TEST(media_cache_same_tokens_different_pixels_cannot_match) {
  Fixture f;
  MediaCachePlan a, b;
  Q4T_CHECK(f.Plan(&a));
  f.items[0].frames[0] = "different pixels with the same geometry";
  Q4T_CHECK(f.Plan(&b));
  Q4T_CHECK(a.token_ends == b.token_ends && a.blocks[0] != b.blocks[0] &&
            a.blocks[1] == b.blocks[1]);
  Q4T_CHECK(!SessionMediaMatches({a.blocks.data(), 1}, b.blocks, 7));
  Q4T_CHECK(MediaCacheKeySuffix(a.blocks, 7) !=
            MediaCacheKeySuffix(b.blocks, 7));
  return true;
}

Q4T_TEST(media_cache_video_temporal_group_identity_and_padding) {
  Fixture f;
  MediaCachePlan a, b;
  Q4T_CHECK(f.Plan(&a));
  f.items[1].frames[2] = "changed last actual frame";
  Q4T_CHECK(f.Plan(&b));
  Q4T_CHECK(a.blocks[0] == b.blocks[0] && a.blocks[1] == b.blocks[1] &&
            a.blocks[2] != b.blocks[2]);
  Q4T_CHECK(SessionMediaMatches({a.blocks.data(), 2}, b.blocks, 12));
  Q4T_CHECK(!SessionMediaMatches(a.blocks, b.blocks, 17));
  // Explicit duplicated frame and temporal padding are separately identified.
  f.items[1].frames.push_back(f.items[1].frames.back());
  f.admission.items[1].frames = 4;
  MediaCachePlan c;
  Q4T_CHECK(f.Plan(&c) && b.blocks[2] != c.blocks[2]);
  return true;
}

Q4T_TEST(media_cache_processor_and_detail_are_part_of_identity) {
  Fixture f;
  MediaCachePlan a;
  Q4T_CHECK(f.Plan(&a));
  for (int mode = 0; mode < 3; ++mode) {
    Fixture changed;
    if (mode == 0) changed.image.image_mean[0] = 0.4f;
    if (mode == 1) changed.image.image_std[2] = 0.7f;
    if (mode == 2) {
      changed.items[0].detail = vision::ImageDetail::kOriginal;
      changed.admission.items[0].detail = vision::ImageDetail::kOriginal;
    }
    MediaCachePlan b;
    Q4T_CHECK(changed.Plan(&b) && a.blocks[0] != b.blocks[0] &&
              a.blocks[1] == b.blocks[1]);
  }
  return true;
}

Q4T_TEST(media_cache_append_media_preserves_only_completed_prefix) {
  Fixture f;
  MediaCachePlan a;
  Q4T_CHECK(f.Plan(&a));
  const std::vector<SessionMediaBlock> first{a.blocks[0]};
  Q4T_CHECK(SessionMediaMatches(first, a.blocks, 7));
  Q4T_CHECK(MediaCacheKeySuffix(first, 7) == MediaCacheKeySuffix(a.blocks, 7));
  Q4T_CHECK(!SessionMediaMatches(first, a.blocks, 12));
  Q4T_CHECK(!SessionMediaMatches({}, a.blocks, 7));
  Q4T_CHECK(SessionMediaMatches({}, a.blocks, 1));
  auto shifted = a.blocks;
  ++shifted[0].begin;
  Q4T_CHECK(!SessionMediaMatches(first, shifted, 7));
  return true;
}

Q4T_TEST(media_cache_partial_and_malformed_blocks_fail_closed) {
  Fixture f;
  MediaCachePlan a;
  Q4T_CHECK(f.Plan(&a));
  for (const int boundary : {-1, 2, 6, 9, 11, 14, 16})
    Q4T_CHECK(SessionMediaPrefix(a.blocks, boundary) == -1);
  for (const int boundary : {0, 1, 7, 8, 12, 13, 17, 18})
    Q4T_CHECK(SessionMediaPrefix(a.blocks, boundary) >= 0);
  auto invalid = a.blocks;
  invalid[1].begin = 6;
  Q4T_CHECK(SessionMediaPrefix(invalid, 18) == -1);
  invalid = a.blocks;
  invalid[0].end = invalid[0].begin;
  Q4T_CHECK(SessionMediaPrefix(invalid, 18) == -1);
  return true;
}

Q4T_TEST(media_cache_plan_rejects_mismatched_admission_and_tokens) {
  for (int mode = 0; mode < 6; ++mode) {
    Fixture f;
    if (mode == 0) f.expanded[4] = 2;
    if (mode == 1) f.items[1].frames.clear();
    if (mode == 2) f.admission.items[1].geometry.grid_t = 0;
    if (mode == 3) f.admission.items[0].geometry.merged_tokens = 99;
    if (mode == 4) f.raw[3] = 1;
    if (mode == 5) --f.admission.expanded_prompt_tokens;
    MediaCachePlan plan;
    Q4T_CHECK(!f.Plan(&plan) && plan.blocks.empty() && plan.token_ends.empty());
  }
  return true;
}

Q4T_TEST(media_cache_source_offsets_preserve_whole_placeholder_clusters) {
  Fixture f;
  MediaCachePlan plan;
  Q4T_CHECK(f.Plan(&plan));
  std::vector<size_t> expanded;
  const std::vector<size_t> raw{0, 2, 9, 12, 20, 23, 30};
  Q4T_CHECK(MediaCacheOffsets(raw, plan, &expanded));
  Q4T_CHECK(expanded.size() == 18 && expanded[1] == 2 && expanded[6] == 2 &&
            expanded[7] == 9 && expanded[8] == 12 && expanded[11] == 12);
  for (const auto& [bytes, expected] :
       {std::pair{2, 1}, {6, 1}, {9, 7}, {12, 8}, {20, 12}, {31, 18}}) {
    int tokens = -1;
    Q4T_CHECK(CompletePromptPrefix(expanded, 31, bytes, &tokens));
    Q4T_CHECK(tokens == expected &&
              SessionMediaPrefix(plan.blocks, tokens) >= 0);
  }
  plan.token_ends[3] = plan.token_ends[2];
  Q4T_CHECK(!MediaCacheOffsets(raw, plan, &expanded) && expanded.empty());
  return true;
}

Q4T_TEST(vision_prefill_slices_keep_absolute_positions_and_feature_order) {
  Fixture f;
  model::VisionPrefillPlan plan;
  const std::vector<std::array<int, 3>> grids{{1, 4, 6}, {1, 4, 4}, {1, 4, 4}};
  Q4T_CHECK(model::PlanVisionPrefill(f.expanded, 2, 1, 2, 24, grids, &plan));
  Q4T_CHECK(plan.prefix_delta[7] == -3 && plan.prefix_delta[12] == -5 &&
            plan.prefix_delta[18] == -7);
  std::vector<int> collected;
  for (int base = 0; base < 18; base += 3) {
    model::VisionPrefillSlice slice;
    Q4T_CHECK(model::SliceVisionPrefill(plan, base, 3, 14, &slice));
    Q4T_CHECK(slice.first_feature == static_cast<int>(collected.size()));
    for (const int local : slice.local_positions) {
      Q4T_CHECK(local >= 0 && local < 3);
      collected.push_back(base + local);
    }
    Q4T_CHECK(slice.rope_delta == plan.prefix_delta[base + 3]);
  }
  Q4T_CHECK(collected == plan.visual_positions);
  // One-shot and all sliced uploads read exactly the same coordinate table.
  std::vector<int> full;
  int delta = 0;
  Q4T_CHECK(model::BuildVisionRopePositions(f.expanded, 2, 1, 2, 24, grids,
                                            &full, &delta));
  Q4T_CHECK(full == plan.positions && delta == plan.prefix_delta.back());
  return true;
}

Q4T_TEST(vision_prefill_restored_suffix_uses_global_feature_cursor) {
  Fixture f;
  model::VisionPrefillPlan plan;
  const std::vector<std::array<int, 3>> grids{{1, 4, 6}, {1, 4, 4}, {1, 4, 4}};
  Q4T_CHECK(model::PlanVisionPrefill(f.expanded, 2, 1, 2, 24, grids, &plan));
  model::VisionPrefillSlice slice;
  Q4T_CHECK(model::SliceVisionPrefill(plan, 7, 6, 14, &slice));
  Q4T_CHECK(slice.first_feature == 6 &&
            slice.local_positions == std::vector<int>({1, 2, 3, 4}) &&
            slice.rope_delta == -5);
  Q4T_CHECK(model::SliceVisionPrefill(plan, 17, 1, 14, &slice));
  Q4T_CHECK(slice.first_feature == 14 && slice.local_positions.empty() &&
            slice.rope_delta == -7);
  return true;
}

Q4T_TEST(vision_prefill_invalid_slice_never_returns_partial_plan) {
  Fixture f;
  model::VisionPrefillPlan plan;
  const std::vector<std::array<int, 3>> grids{{1, 4, 6}, {1, 4, 4}, {1, 4, 4}};
  Q4T_CHECK(model::PlanVisionPrefill(f.expanded, 2, 1, 2, 24, grids, &plan));
  for (const auto& range :
       {std::array<int, 3>{-1, 3, 14}, {0, 0, 14}, {17, 2, 14}, {0, 3, 13}}) {
    model::VisionPrefillSlice slice;
    Q4T_CHECK(
        !model::SliceVisionPrefill(plan, range[0], range[1], range[2], &slice));
    Q4T_CHECK(slice.local_positions.empty() && slice.first_feature == 0);
  }
  Q4T_CHECK(!model::PlanVisionPrefill(f.expanded, 2, 1, 2, 24, {}, &plan));
  Q4T_CHECK(plan.ids.empty() && plan.positions.empty());
  return true;
}
