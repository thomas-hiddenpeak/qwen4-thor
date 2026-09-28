#include "q4t/model/model_owner.h"

namespace q4t::model {

ModelOwner::~ModelOwner() { model_.Free(); }

Status ModelOwner::Load(const ModelConfig& cfg, cudaStream_t stream) {
  if (loaded_) return Status::Fail("ModelOwner: model already loaded");
  Status status = LoadModel(cfg, &model_, stream);
  const cudaError_t completion = cudaStreamSynchronize(stream);
  if (completion != cudaSuccess) {
    status = Status::Fail(std::string("ModelOwner load completion: ") +
                          cudaGetErrorString(completion));
  }
  if (!status.ok()) {
    model_.Free();
    return status;
  }
  loaded_ = true;
  return Status();
}

}  // namespace q4t::model
