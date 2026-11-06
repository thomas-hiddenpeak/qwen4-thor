#include "q4t/test.h"
#include "q4t/vision/configuration.h"

Q4T_TEST(vision_configuration_rejects_silent_architecture_drift) {
  using q4t::io::Json;
  using q4t::io::ParseJson;
  using q4t::vision::ValidateVisionConfiguration;
  Json model, image, video;
  Q4T_CHECK(ParseJson(R"({"vision_config":{"depth":27,"hidden_size":1152,
    "num_heads":16,"intermediate_size":4304,"patch_size":16,
    "spatial_merge_size":2,"temporal_patch_size":2,"in_channels":3,
    "out_hidden_size":2560,"num_position_embeddings":2304,
    "deepstack_visual_indexes":[],"hidden_act":"gelu_pytorch_tanh"},
    "image_token_id":248056,"video_token_id":248057,
    "vision_start_token_id":248053,"vision_end_token_id":248054})",
                      &model));
  const std::string common = R"("patch_size":16,"merge_size":2,
    "temporal_patch_size":2,"image_mean":[0.5,0.5,0.5],
    "image_std":[0.5,0.5,0.5])";
  Q4T_CHECK(ParseJson("{" + common + R"(,"size":{"shortest_edge":65536,
    "longest_edge":16777216}})",
                      &image));
  Q4T_CHECK(ParseJson("{" + common + R"(,"size":{"shortest_edge":4096,
    "longest_edge":25165824}})",
                      &video));
  Q4T_CHECK(ValidateVisionConfiguration(model, image, video));
  Q4T_CHECK(!ValidateVisionConfiguration(model, video, image));
  for (const char* extra :
       {R"("do_resize":false)", R"("resample":2)", R"("image_mean":[0,0,0])",
        R"("rescale_factor":1)", R"("merge_size":4)"}) {
    Json invalid;
    // Json lookup is first-match; put the mutation first intentionally.
    Q4T_CHECK(ParseJson(
        std::string("{") + extra + "," + common +
            R"(,"size":{"shortest_edge":65536,"longest_edge":16777216}})",
        &invalid));
    Q4T_CHECK(!ValidateVisionConfiguration(model, invalid, video));
  }
  for (auto& [key, value] : model.object) {
    if (key == "image_token_id") {
      value.number = 1;
      break;
    }
  }
  Q4T_CHECK(!ValidateVisionConfiguration(model, image, video));
  return true;
}
