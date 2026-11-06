#include "q4t/model/vision_positions.h"

#include "q4t/test.h"

namespace {
constexpr int kImage = 1, kVideo = 2;
using Grid = std::array<int, 3>;
}  // namespace

Q4T_TEST(vision_positions_text_and_special_text) {
  const std::vector<int32_t> ids{9, kImage, 8, kVideo, 7};
  std::vector<int> positions;
  int delta = -10;
  Q4T_CHECK(q4t::model::BuildVisionRopePositions(ids, 2, kImage, kVideo, 8, {},
                                                 &positions, &delta));
  Q4T_CHECK(delta == 0 && positions.size() == 24);
  for (int axis = 0; axis < 3; ++axis) {
    for (int p = 0; p < 8; ++p)
      Q4T_CHECK(positions[axis * 8 + p] == (p < 5 ? p : 0));
  }
  return true;
}

Q4T_TEST(vision_positions_image_end_uses_all_axes) {
  // An image at the very end: width, not t, determines decode's next clock.
  const std::vector<int32_t> ids{9,      kImage, kImage, kImage,
                                 kImage, kImage, kImage};
  const std::vector<Grid> grids{{1, 4, 6}};
  std::vector<int> positions;
  int delta;
  Q4T_CHECK(q4t::model::BuildVisionRopePositions(ids, 2, kImage, kVideo, 7,
                                                 grids, &positions, &delta));
  const std::vector<int> expected{0, 1, 1, 1, 1, 1, 1, 0, 1, 1, 1,
                                  2, 2, 2, 0, 1, 2, 3, 1, 2, 3};
  Q4T_CHECK(positions == expected && delta == -3);
  return true;
}

Q4T_TEST(vision_positions_timestamped_video_groups) {
  // Distinct text runs stand in for actual timestamp/boundary tokenization.
  const std::vector<int32_t> ids{9, kVideo, kVideo, 8, 7, kVideo, kVideo, 6};
  const std::vector<Grid> grids{{1, 2, 4}, {1, 2, 4}};
  std::vector<int> positions;
  int delta;
  Q4T_CHECK(q4t::model::BuildVisionRopePositions(ids, 2, kImage, kVideo, 8,
                                                 grids, &positions, &delta));
  const std::vector<int> expected{0, 1, 1, 3, 4, 5, 5, 7, 0, 1, 1, 3,
                                  4, 5, 5, 7, 0, 1, 2, 3, 4, 5, 6, 7};
  Q4T_CHECK(positions == expected && delta == 0);
  return true;
}

Q4T_TEST(vision_positions_invalid_grids_fail_closed) {
  const std::vector<int32_t> ids{9, kImage, kImage, 8};
  for (const auto& grids :
       std::vector<std::vector<Grid>>{{{0, 2, 4}},
                                      {{2, 2, 2}},
                                      {{1, 3, 4}},
                                      {{1, 0, 4}},
                                      {{1, 2, 8}},
                                      {{1, 2, 2}},
                                      {{1, 2, 4}, {1, 2, 2}},
                                      {{1, 2147483646, 2147483646}}}) {
    std::vector<int> positions{99};
    int delta = 99;
    Q4T_CHECK(!q4t::model::BuildVisionRopePositions(ids, 2, kImage, kVideo, 4,
                                                    grids, &positions, &delta));
    Q4T_CHECK(positions.empty() && delta == 0);
  }
  std::vector<int> positions;
  int delta;
  const std::vector<Grid> grids{{1, 2, 4}};
  const std::vector<int32_t> mixed{9, kImage, kVideo, 8};
  Q4T_CHECK(!q4t::model::BuildVisionRopePositions(mixed, 2, kImage, kVideo, 4,
                                                  grids, &positions, &delta));
  Q4T_CHECK(!q4t::model::BuildVisionRopePositions(ids, 2, kImage, kVideo, 3,
                                                  grids, &positions, &delta));
  return true;
}
