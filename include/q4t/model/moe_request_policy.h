// Immutable request-scoped prefill partition selection. The complete tokenized
// prompt selects the policy; a continuation's row count never selects it.
#pragma once

#include <cstring>
#include <string>
#include <utility>

namespace q4t::model {

inline int ParseMoERequestPartitionMode(const char* value) {
  if (value == nullptr || std::strcmp(value, "0") == 0) return 0;
  if (std::strcmp(value, "1") == 0) return 1;
  return -1;
}

class MoERequestPartition {
 public:
  // An omitted context preserves Q4T_MOE_PARTITION for existing model callers.
  MoERequestPartition() = default;
  // Inactive identity enables symmetric HTTP logs without changing global mode.
  MoERequestPartition(int request_tokens, std::string request_id,
                      bool enabled = true)
      : request_tokens_(request_tokens), request_id_(std::move(request_id)),
        enabled_(enabled) {}

  bool Enabled() const { return enabled_; }
  bool HasRequest() const { return request_tokens_ != 0; }
  int RequestTokens() const { return request_tokens_; }
  const std::string& RequestId() const { return request_id_; }
  int Base() const { return base_; }
  int PartitionMode(int global_mode) const {
    return Enabled() ? (request_tokens_ > 8192 ? 1 : 0) : global_mode;
  }
  MoERequestPartition WithBase(int base) const {
    return HasRequest()
               ? MoERequestPartition(request_tokens_, request_id_, enabled_,
                                     base)
               : *this;
  }
  bool ValidForward(int tokens) const {
    if (!HasRequest())
      return !enabled_ && request_id_.empty() && base_ == 0;
    if (request_tokens_ <= 0 || base_ < 0 || base_ >= request_tokens_ ||
        tokens <= 0 || tokens > request_tokens_ - base_ ||
        request_id_.empty() || request_id_.size() > 128) {
      return false;
    }
    // IDs appear in a single ordinary log field, just like HTTP request IDs.
    for (const unsigned char c : request_id_) {
      if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
            (c >= '0' && c <= '9') || c == '-' || c == '_')) {
        return false;
      }
    }
    return true;
  }

 private:
  MoERequestPartition(int request_tokens, std::string request_id, bool enabled,
                      int base)
      : request_tokens_(request_tokens), request_id_(std::move(request_id)),
        enabled_(enabled), base_(base) {}

  const int request_tokens_ = 0;
  const std::string request_id_;
  const bool enabled_ = false;  // True only for the request-length override.
  const int base_ = 0;
};

}  // namespace q4t::model
