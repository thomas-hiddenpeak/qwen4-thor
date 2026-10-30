// Check the consumer's NVFP4 layout before allocating or reading payloads.
#pragma once

#include <string>
#include <string_view>

#include "q4t/io/weight_loader.h"

namespace q4t::quant {

inline Status ValidateMoEExpert(const io::WeightLoader& loader, int layer,
                                int expert, int hs, int moe_is) {
  const std::string prefix = "model.language_model.layers." +
                             std::to_string(layer) + ".mlp.experts." +
                             std::to_string(expert) + ".";
  for (const char* projection : {"down_proj", "gate_proj", "up_proj"}) {
    const bool down = projection[0] == 'd';
    const int rows = down ? hs : moe_is;
    const int cols = down ? moe_is : hs;
    for (const char* suffix :
         {"weight", "weight_scale", "weight_scale_2", "input_scale"}) {
      const std::string name = prefix + projection + "." + suffix;
      const auto* info = loader.FindTensor(name);
      if (!info) return Status::Fail("missing MoE tensor: " + name);
      const std::string_view kind(suffix);
      if (kind == "weight" || kind == "weight_scale") {
        const bool packed = kind == "weight";
        const int width = cols / (packed ? 2 : 16);
        const auto dtype = packed ? io::Dtype::kU8 : io::Dtype::kF8E4M3;
        if (info->dtype != dtype || info->shape.size() != 2 ||
            info->shape[0] != rows || info->shape[1] != width ||
            info->byte_size() != static_cast<size_t>(rows) * width) {
          return Status::Fail("MoE tensor dtype/shape/size mismatch: " + name);
        }
      } else {
        // Checkpoints may encode a scalar as rank zero or a one-element
        // vector. Both represent exactly one FP32 value; no wider ranks.
        const bool scalar = info->shape.empty() ||
                            (info->shape.size() == 1 && info->shape[0] == 1);
        if (info->dtype != io::Dtype::kF32 || !scalar ||
            info->byte_size() != sizeof(float)) {
          return Status::Fail("MoE scalar dtype/shape/size mismatch: " + name);
        }
      }
    }
  }
  return Status();
}

}  // namespace q4t::quant
