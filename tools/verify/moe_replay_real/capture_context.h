// Explicit caller-owned mapping for a single synchronous model invocation.
// GPU completion is the wrapper/driver's responsibility, not inferred here.
#pragma once

#include <algorithm>
#include <cstdint>
#include <span>
#include <vector>

#include "q4t/status.h"

namespace q4t::diagnostic {

class CaptureContext {
 public:
  Status Begin(std::span<const int32_t> prefix, int position, int rows,
               int layers, int target_layer, int vocab) {
    if (active_) return Fail("capture: invocation already active");
    valid_ = false;
    failed_ = false;
    captured_ = false;
    tokens_.clear();
    seen_.clear();
    if (position < 0 || rows <= 0 || layers <= 0 || layers > 48 ||
        target_layer < 0 || target_layer >= layers || vocab <= 0 ||
        static_cast<size_t>(position) > prefix.size() ||
        prefix.size() - static_cast<size_t>(position) !=
            static_cast<size_t>(rows) ||
        std::any_of(prefix.begin(), prefix.end(),
                    [vocab](int32_t token) { return token < 0 || token >= vocab; })) {
      return Fail("capture: invalid prefix/position/shape/layer");
    }
    tokens_.assign(prefix.begin(), prefix.end());
    seen_.assign(layers, false);
    position_ = position;
    rows_ = rows;
    target_layer_ = target_layer;
    active_ = true;
    return {};
  }

  // Observe the actual layer_id supplied by DecoderLayerForward. Layer IDs,
  // not the call count, determine the capture destination.
  Status ObserveLayer(int layer, int rows) {
    if (!active_ || failed_ || layer < 0 ||
        static_cast<size_t>(layer) >= seen_.size() || rows != rows_ ||
        seen_[layer]) {
      return Fail("capture: missing context/duplicate layer/shape mismatch");
    }
    seen_[layer] = true;
    return {};
  }

  // Call only after the selected layer's original forward and capture copies
  // have completed successfully on its stream.
  Status Captured(int layer) {
    if (!active_ || failed_ || captured_ || layer != target_layer_ ||
        !seen_[target_layer_]) {
      return Fail("capture: unexpected capture completion");
    }
    captured_ = true;
    return {};
  }

  Status Finish(bool execution_ok) {
    valid_ = active_ && !failed_ && execution_ok && captured_ &&
             std::all_of(seen_.begin(), seen_.end(), [](bool value) {
               return value;
             });
    active_ = false;
    if (!valid_) return Fail("capture: incomplete or failed invocation");
    return {};
  }

  bool active() const { return active_; }
  bool valid() const { return valid_; }
  int target_layer() const { return target_layer_; }
  int position() const { return position_; }
  int rows() const { return rows_; }
  const std::vector<int32_t>& tokens() const { return tokens_; }

 private:
  Status Fail(const char* message) {
    failed_ = true;
    valid_ = false;
    return Status::Fail(message);
  }
  bool active_ = false;
  bool failed_ = false;
  bool captured_ = false;
  bool valid_ = false;
  int target_layer_ = -1;
  int position_ = 0;
  int rows_ = 0;
  std::vector<int32_t> tokens_;
  std::vector<bool> seen_;
};

}  // namespace q4t::diagnostic
