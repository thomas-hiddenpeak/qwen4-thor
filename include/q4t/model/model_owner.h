// Owns the existing Model allocations; Model references remain borrowed views.
#pragma once

#include "q4t/model/model.h"

namespace q4t::model {

// The caller must stop users and complete GPU work before destroying this
// owner. MTP borrows model weights and must be released first. This owner does
// not introduce asynchronous reclamation or transfer scratch ownership.
class ModelOwner final {
 public:
  ModelOwner() = default;
  ~ModelOwner();
  ModelOwner(const ModelOwner&) = delete;
  ModelOwner& operator=(const ModelOwner&) = delete;
  ModelOwner(ModelOwner&&) = delete;
  ModelOwner& operator=(ModelOwner&&) = delete;

  // A failed load drains its stream and releases partial allocations. Loading
  // over a live model is rejected rather than invalidating borrowed views.
  Status Load(const ModelConfig& cfg, cudaStream_t stream);
  Model& Get() { return model_; }
  const Model& Get() const { return model_; }

 private:
  Model model_;
  bool loaded_ = false;
};

}  // namespace q4t::model
