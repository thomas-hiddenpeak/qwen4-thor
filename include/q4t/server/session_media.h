#pragma once

#include <algorithm>
#include <span>

#include "q4t/trace/sha256.h"

namespace q4t::server {

// Coordinates refer to expanded prompt tokens; [begin, end) is one complete
// visual block. Identity binds source bytes and the effective processor.
struct SessionMediaBlock {
  int begin = 0;
  int end = 0;
  trace::Digest identity{};
  bool operator==(const SessionMediaBlock&) const = default;
};

// Returns the count of complete prefix blocks, or -1 for invalid metadata or
// a boundary inside a block. Later media may be appended without changing the
// identity of the prefix already represented by model state.
inline int SessionMediaPrefix(std::span<const SessionMediaBlock> media,
                              int prefix) {
  if (prefix < 0 || media.size() > 128) return -1;
  int previous = 0, count = 0;
  for (const auto& block : media) {
    if (block.begin < previous || block.end <= block.begin ||
        (block.begin < prefix && prefix < block.end))
      return -1;
    previous = block.end;
    if (block.end <= prefix) ++count;
  }
  return count;
}

inline bool SessionMediaMatches(std::span<const SessionMediaBlock> cached,
                                std::span<const SessionMediaBlock> requested,
                                int prefix) {
  const int count = SessionMediaPrefix(requested, prefix);
  return count >= 0 && static_cast<size_t>(count) == cached.size() &&
         SessionMediaPrefix(cached, prefix) == count &&
         std::equal(cached.begin(), cached.end(), requested.begin());
}

}  // namespace q4t::server
