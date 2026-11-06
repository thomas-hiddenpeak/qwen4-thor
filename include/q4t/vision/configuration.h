// Fixed-checkpoint vision configuration gate, with no CUDA dependency.
#pragma once

#include <fstream>
#include <string>
#include <utility>

#include "q4t/io/json.h"

namespace q4t::vision {

inline Status ValidateVisionConfiguration(const io::Json& model,
                                          const io::Json& image,
                                          const io::Json& video) {
  const auto fail = [] {
    return Status::Fail("unsupported fixed-checkpoint vision configuration");
  };
  const auto* cfg = model.Find("vision_config");
  if (!cfg || !cfg->IsObject()) return fail();
  for (const auto& [key, expected] : {std::pair{"depth", 27},
                                      {"hidden_size", 1152},
                                      {"num_heads", 16},
                                      {"intermediate_size", 4304},
                                      {"patch_size", 16},
                                      {"spatial_merge_size", 2},
                                      {"temporal_patch_size", 2},
                                      {"in_channels", 3},
                                      {"out_hidden_size", 2560},
                                      {"num_position_embeddings", 2304}}) {
    if (cfg->GetInt(key, -1) != expected) return fail();
  }
  const auto* deepstack = cfg->GetArray("deepstack_visual_indexes");
  if (!deepstack || !deepstack->array.empty() ||
      cfg->GetString("hidden_act") != "gelu_pytorch_tanh")
    return fail();
  for (const auto& [key, expected] : {std::pair{"image_token_id", 248056},
                                      {"video_token_id", 248057},
                                      {"vision_start_token_id", 248053},
                                      {"vision_end_token_id", 248054}})
    if (model.GetInt(key, -1) != expected) return fail();
  for (const auto* processor : {&image, &video}) {
    if (processor->GetInt("patch_size") != 16 ||
        processor->GetInt("merge_size") != 2 ||
        processor->GetInt("temporal_patch_size") != 2)
      return fail();
    for (const auto* key : {"image_mean", "image_std"}) {
      const auto* values = processor->GetArray(key);
      if (!values || values->array.size() != 3) return fail();
      for (const auto& value : values->array)
        if (!value.IsNumber() || value.number != 0.5) return fail();
    }
    const auto* size = processor->Find("size");
    if (!size ||
        size->GetInt("shortest_edge") != (processor == &image ? 65536 : 4096) ||
        size->GetInt("longest_edge") !=
            (processor == &image ? 16777216 : 25165824))
      return fail();
    // Explicit changes to preprocessing cannot silently retain defaults.
    for (const char* key :
         {"do_resize", "do_rescale", "do_normalize", "do_convert_rgb"}) {
      const auto* value = processor->Find(key);
      if (value && (!value->IsBool() || !value->boolean)) return fail();
    }
    const auto* resample = processor->Find("resample");
    if (resample && resample->AsInt(-1) != 3) return fail();
    const auto* factor = processor->Find("rescale_factor");
    if (factor && (!factor->IsNumber() || factor->number != 1.0 / 255.0))
      return fail();
  }
  return Status();
}

inline Status CheckVisionConfiguration(const std::string& directory) {
  io::Json configs[3];
  int index = 0;
  for (const char* name : {"config.json", "preprocessor_config.json",
                           "video_preprocessor_config.json"}) {
    std::ifstream input(directory + "/" + name,
                        std::ios::binary | std::ios::ate);
    const auto size = input.tellg();
    if (!input || size <= 0 || size > 1024 * 1024)
      return Status::Fail(std::string("invalid vision configuration file: ") +
                          name);
    std::string data(static_cast<size_t>(size), '\0');
    input.seekg(0);
    if (!input.read(data.data(), size))
      return Status::Fail(std::string("cannot read vision configuration: ") +
                          name);
    io::JsonParseLimits limits;
    limits.max_string_bytes = 1024 * 1024;
    limits.reject_duplicate_keys = true;
    auto status = io::ParseJson(data, &configs[index++], false, limits);
    if (!status) return status;
  }
  return ValidateVisionConfiguration(configs[0], configs[1], configs[2]);
}

}  // namespace q4t::vision
