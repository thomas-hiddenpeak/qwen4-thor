#include "q4t/model/model_owner.h"

#include "q4t/runtime/cleanup_policy.h"

namespace q4t::model {

ModelOwner::~ModelOwner() { model_.Free(); }

Status ModelOwner::Load(const ModelConfig& cfg, cudaStream_t stream) {
  if (loaded_) return Status::Fail("ModelOwner: model already loaded");
  Status status = LoadModel(cfg, &model_, stream);
  const runtime::CleanupPhase phase("model.owner_load");
  const cudaError_t completion = cudaStreamSynchronize(stream);
  runtime::RequireCleanupSuccess(completion, "model.owner_load completion");
  if (!status.ok()) {
    model_.Free();
    return status;
  }
  loaded_ = true;
  return Status();
}

}  // namespace q4t::model
