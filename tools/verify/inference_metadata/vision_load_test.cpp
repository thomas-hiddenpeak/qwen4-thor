// One real tower load; independently read back every uploaded weight payload.
#include "q4t/io/weight_loader.h"
#include "q4t/trace/sha256.h"
#include "q4t/vision/vision.h"

#include <iostream>
#include <memory>
#include <stdexcept>
#include <vector>

namespace {
struct Upload {
  const void* device;
  size_t bytes;
  q4t::trace::Digest expected;
};
std::vector<Upload> uploads;
void Require(bool condition) {
  if (!condition) throw std::runtime_error("vision upload contract failed");
}
}  // namespace

extern "C" cudaError_t __real_cudaMemcpyAsync(
    void*, const void*, size_t, cudaMemcpyKind, cudaStream_t);
extern "C" cudaError_t __wrap_cudaMemcpyAsync(
    void* dst, const void* src, size_t bytes, cudaMemcpyKind kind,
    cudaStream_t stream) {
  if (kind == cudaMemcpyHostToDevice)
    uploads.push_back({dst, bytes, q4t::trace::Sha256(
        {static_cast<const uint8_t*>(src), bytes})});
  return __real_cudaMemcpyAsync(dst, src, bytes, kind, stream);
}

int main(int argc, char** argv) {
  try {
    Require(argc == 2);
    q4t::io::WeightIndex* raw_index = nullptr;
    Require(q4t::io::WeightIndex::Open(std::string(argv[1]) +
        "/model.safetensors.index.json", &raw_index).ok());
    std::unique_ptr<q4t::io::WeightIndex> index(raw_index);
    q4t::io::WeightLoader* raw_loader = nullptr;
    Require(q4t::io::WeightLoader::Create(argv[1], *index, 8,
                                          &raw_loader).ok());
    std::unique_ptr<q4t::io::WeightLoader> loader(raw_loader);
    q4t::vision::VisionTower tower;
    std::string error;
    Require(q4t::vision::LoadVision(*loader, {}, &tower, &error, nullptr));
    Require(!uploads.empty());
    size_t total = 0;
    for (const auto& upload : uploads) {
      std::vector<uint8_t> host(upload.bytes);
      Require(cudaMemcpy(host.data(), upload.device, host.size(),
                          cudaMemcpyDeviceToHost) == cudaSuccess);
      Require(q4t::trace::Sha256(host) == upload.expected);
      total += host.size();
    }
    Require(cudaStreamSynchronize(nullptr) == cudaSuccess);
    std::cout << "PASS uploads=" << uploads.size() << " bytes=" << total
              << " semantic_validation=false\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "FAIL: " << error.what() << '\n';
    return 1;
  }
}
