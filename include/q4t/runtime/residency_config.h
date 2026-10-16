// Shared host-only interpretation of per-layer residency capacity and fill.
#pragma once

#include <algorithm>
#include <charconv>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::runtime {

struct ResidencyConfig {
  std::vector<std::vector<int>> hot_lists;
  std::vector<int> layer_slots;
  size_t total_slots = 0;
};

// Missing or empty layers retain the global slot capacity with no hot fill.
// Nonempty layers use min(list length, cap), preserving loader behavior.
inline Status ParseResidencyConfig(const std::string& text, int layers,
                                   int experts, int cap, ResidencyConfig* out) {
  if (!out || layers < 0 || experts <= 0 || cap < 0 || cap > experts) {
    return Status::Fail("invalid residency configuration dimensions");
  }
  ResidencyConfig result;
  result.hot_lists.resize(layers);
  result.layer_slots.assign(layers, cap);
  if (cap > 0 && !text.empty()) {
    io::Json doc;
    io::JsonParseLimits limits;
    limits.reject_duplicate_keys = true;
    Status s = io::ParseJson(text, &doc, false, limits);
    if (!s.ok() || !doc.IsObject()) {
      return Status::Fail("invalid moe-hot-list JSON (want object)");
    }
    std::vector<bool> seen(layers, false);
    for (const auto& [key, value] : doc.object) {
      int layer = -1;
      const char* end = key.data() + key.size();
      const auto parsed = std::from_chars(key.data(), end, layer);
      if (parsed.ec != std::errc() || parsed.ptr != end || layer < 0 ||
          layer >= layers) {
        return Status::Fail("invalid moe-hot-list layer: " + key);
      }
      if (seen[layer] || !value.IsArray()) {
        return Status::Fail("duplicate or non-array moe-hot-list layer: " +
                            key);
      }
      seen[layer] = true;
      auto& hot = result.hot_lists[layer];
      for (const io::Json& expert : value.array) {
        const int64_t id = expert.AsInt(-1);
        if (!expert.IsNumber() || id < 0 || id >= experts) {
          return Status::Fail("invalid moe-hot-list expert: " + key);
        }
        hot.push_back(static_cast<int>(id));
      }
      if (!hot.empty()) {
        result.layer_slots[layer] =
            static_cast<int>(std::min<size_t>(hot.size(), cap));
      }
    }
  }
  for (int slots : result.layer_slots) result.total_slots += slots;
  *out = std::move(result);
  return Status();
}

inline Status LoadResidencyConfig(const std::string& path, int layers,
                                  int experts, int cap, ResidencyConfig* out) {
  if (cap == 0 || path.empty()) {
    return ParseResidencyConfig("", layers, experts, cap, out);
  }
  std::ifstream input(path);
  if (!input) return Status::Fail("cannot open moe-hot-list: " + path);
  const std::string text{std::istreambuf_iterator<char>(input),
                         std::istreambuf_iterator<char>()};
  if (input.bad() || text.empty()) {
    return Status::Fail("cannot read moe-hot-list: " + path);
  }
  return ParseResidencyConfig(text, layers, experts, cap, out);
}

}  // namespace q4t::runtime
