#pragma once

#include <cstdint>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {
// Derived metadata only: never an alternate source of durable object content.
struct ObjectRetentionInfo {
  bool active = false;
  bool uncertain = false;
  std::vector<std::string> references;
  // Immutable internal binary fragments have no independent lifetime. Only
  // a live staging/file/upload/read root can retain them.
  bool collect_when_unreferenced = false;
  size_t Bytes() const;
};

ObjectRetentionInfo InspectObjectRetention(std::string_view kind,
                                           const io::Json& data);

struct ObjectRetentionNode {
  std::string_view owner, kind, id, digest;
  int64_t created = 0, expires = 0;
  uint64_t bytes = 0;
  const ObjectRetentionInfo* retention = nullptr;
};

struct ObjectRetentionPlan {
  std::vector<size_t> selected;
  size_t eligible = 0;
  size_t active = 0;
  size_t referenced = 0;
  size_t uncertain = 0;
  uint64_t bytes = 0;
  std::string sha256;
};

// Input ordering is stable; one plan selects at most limit records belonging
// to one owner so the existing atomic transaction protocol can apply it.
// Permanent unreferenced items are candidates only with an explicit cutoff.
Status PlanObjectRetention(std::span<const ObjectRetentionNode> nodes,
                           int64_t now, int64_t item_created_before,
                           size_t limit, ObjectRetentionPlan* plan);
}  // namespace q4t::server
