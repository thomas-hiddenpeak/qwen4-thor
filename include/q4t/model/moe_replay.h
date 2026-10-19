// Host-side contract for the standalone main-model route diagnostic.
#pragma once

#include <algorithm>
#include <cstdint>
#include <span>
#include <string>
#include <vector>

#include "q4t/status.h"
#include "q4t/trace/router_trace.h"

namespace q4t::model {

struct MoEReplayIdentity {
  trace::Digest model{};
  trace::Digest weights{};
  trace::Digest hidden{};
  trace::Digest binary{};
  std::string role = "main";
  int layer = -1;
  uint64_t position = 0;
  int rows = 0;
  int experts = 0;
  int top_k = 0;
  // Exact prefix through the final input row (not generated output tokens).
  std::vector<int32_t> tokens;
};

struct MoEReplayCompatibility {
  trace::Digest source_binary{};
  trace::Digest execution_binary{};
  std::string allowed_difference;
};

// Owns IDs after validation. No caller-owned span survives Prepare(). A failed
// Prepare invalidates the previous snapshot. GPU owners must retain their own
// upload snapshot until stream completion; this type performs no CUDA work.
class MoEReplayRecord {
 public:
  Status Prepare(MoEReplayIdentity source,
                 MoEReplayIdentity execution,
                 std::span<const int32_t> ids,
                 const MoEReplayCompatibility* compatibility = nullptr) {
    const std::vector<int32_t> owned_ids(ids.begin(), ids.end());
    ids = owned_ids;
    ids_.clear();
    source_ = {};
    execution_ = {};
    const auto present = [](const trace::Digest& digest) {
      return std::any_of(digest.begin(), digest.end(),
                         [](uint8_t value) { return value != 0; });
    };
    for (const auto* identity : {&source, &execution}) {
      if (!present(identity->model) || !present(identity->weights) ||
          !present(identity->hidden) || !present(identity->binary)) {
        return Status::Fail("replay: missing identity");
      }
      if (identity->role != "main" || identity->layer < 0 ||
          identity->rows <= 0 || identity->experts <= 0 ||
          identity->experts > 512 || identity->top_k <= 0 ||
          identity->top_k > 16 || identity->top_k > identity->experts) {
        return Status::Fail("replay: invalid role/layer/shape");
      }
      const auto rows = static_cast<uint64_t>(identity->rows);
      if (identity->position > identity->tokens.size() ||
          rows != identity->tokens.size() - identity->position ||
          std::any_of(identity->tokens.begin(), identity->tokens.end(),
                      [](int32_t token) { return token < 0; })) {
        return Status::Fail("replay: incomplete token prefix");
      }
    }
    if (source.model != execution.model || source.weights != execution.weights ||
        source.hidden != execution.hidden || source.role != execution.role ||
        source.layer != execution.layer || source.position != execution.position ||
        source.rows != execution.rows || source.experts != execution.experts ||
        source.top_k != execution.top_k || source.tokens != execution.tokens) {
      return Status::Fail("replay: execution identity mismatch");
    }
    if (source.binary != execution.binary &&
        (!compatibility || compatibility->source_binary != source.binary ||
         compatibility->execution_binary != execution.binary ||
         compatibility->allowed_difference.empty())) {
      return Status::Fail("replay: binary difference not explicitly allowed");
    }
    const size_t k = static_cast<size_t>(source.top_k);
    if (ids.size() != static_cast<size_t>(source.rows) * k) {
      return Status::Fail("replay: missing or extra IDs");
    }
    for (size_t row = 0; row < ids.size(); row += k) {
      for (size_t slot = 0; slot < k; ++slot) {
        const int32_t id = ids[row + slot];
        if (id < 0 || id >= source.experts) {
          return Status::Fail("replay: expert out of range");
        }
        for (size_t previous = 0; previous < slot; ++previous) {
          if (ids[row + previous] == id) {
            return Status::Fail("replay: duplicate expert");
          }
        }
      }
    }
    ids_.assign(ids.begin(), ids.end());
    source_ = source;
    execution_ = execution;
    return {};
  }

  bool ready() const { return !ids_.empty(); }
  std::span<const int32_t> ids() const { return ids_; }
  const MoEReplayIdentity& source() const { return source_; }
  const MoEReplayIdentity& execution() const { return execution_; }

 private:
  std::vector<int32_t> ids_;
  MoEReplayIdentity source_;
  MoEReplayIdentity execution_;
};

}  // namespace q4t::model
