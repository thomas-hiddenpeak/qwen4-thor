// Delayed-copy ownership checks; --gpu additionally checks real CUDA streams.
#include "q4t/io/pending_host_storage.h"
#include "q4t/io/weight_loader.h"

#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <vector>

namespace {
bool real_cuda = false;
bool fail_copy = false;
int failed_waits = 0, calls = 0, waits = 0;
struct Copy { void* dst; const void* src; size_t bytes; cudaStream_t stream; };
std::vector<Copy> pending;
void Require(bool value) {
  if (!value) throw std::runtime_error("load source contract failed");
}
void Cuda(cudaError_t error) { Require(error == cudaSuccess); }
void CheckBytes(const std::vector<uint8_t>& bytes) {
  Require(bytes == std::vector<uint8_t>({1, 7, 19, 33, 55, 81, 117, 255}));
}
}  // namespace

extern "C" cudaError_t __real_cudaMemcpyAsync(
    void*, const void*, size_t, cudaMemcpyKind, cudaStream_t);
extern "C" cudaError_t __real_cudaStreamSynchronize(cudaStream_t);
extern "C" cudaError_t __wrap_cudaMemcpyAsync(
    void* dst, const void* src, size_t bytes, cudaMemcpyKind kind,
    cudaStream_t stream) {
  if (real_cuda) return __real_cudaMemcpyAsync(dst, src, bytes, kind, stream);
  Require(kind == cudaMemcpyHostToDevice);
  ++calls;
  pending.push_back({dst, src, bytes, stream});
  return fail_copy ? cudaErrorUnknown : cudaSuccess;
}
extern "C" cudaError_t __wrap_cudaStreamSynchronize(cudaStream_t stream) {
  if (real_cuda) return __real_cudaStreamSynchronize(stream);
  ++waits;
  if (failed_waits > 0) {
    --failed_waits;
    return cudaErrorUnknown;
  }
  for (const auto& copy : pending) {
    Require(copy.stream == stream);
    std::memcpy(copy.dst, copy.src, copy.bytes);
  }
  pending.clear();
  return cudaSuccess;
}

int main(int argc, char** argv) {
  try {
    Require(argc == 2 || (argc == 3 && std::string(argv[2]) == "--gpu"));
    const auto path = std::filesystem::weakly_canonical(argv[1]);
    const auto relative = path.lexically_relative(std::filesystem::current_path());
    Require(!relative.empty() && (*relative.begin() == ".q4t-work" ||
                                  *relative.begin() == "build"));
    Require(std::filesystem::create_directory(path));
    const std::string header =
        R"({"w":{"dtype":"U8","shape":[8],"data_offsets":[0,8]}})";
    const uint64_t length = header.size();
    const std::vector<uint8_t> bytes{1, 7, 19, 33, 55, 81, 117, 255};
    std::ofstream file(path / "weights.safetensors", std::ios::binary);
    file.write(reinterpret_cast<const char*>(&length), sizeof(length));
    file << header;
    file.write(reinterpret_cast<const char*>(bytes.data()), bytes.size());
    file.close();
    Require(bool(file));
    std::ofstream index_file(path / "index.json");
    index_file << R"({"weight_map":{"w":"weights.safetensors"}})";
    index_file.close();
    Require(bool(index_file));
    q4t::io::WeightIndex* raw_index = nullptr;
    Require(q4t::io::WeightIndex::Open((path / "index.json").string(),
                                      &raw_index).ok());
    std::unique_ptr<q4t::io::WeightIndex> index(raw_index);
    q4t::io::WeightLoader* raw_loader = nullptr;
    Require(q4t::io::WeightLoader::Create(path.string(), *index, 1,
                                          &raw_loader).ok());
    std::unique_ptr<q4t::io::WeightLoader> loader(raw_loader);
    int cases = 0;
    const auto other = reinterpret_cast<cudaStream_t>(uintptr_t{1});
    for (auto stream : {cudaStream_t{}, other}) {
      std::vector<uint8_t> output(8, 0);
      Require(loader->LoadTensorToDevice("w", output.data(), 8, stream).ok());
      Require(pending.empty());
      CheckBytes(output);
      ++cases;
    }
    std::vector<uint8_t> output(8, 0);
    const int before = calls;
    Require(!loader->LoadTensorToDevice("w", output.data(), 7, nullptr).ok());
    Require(!loader->LoadTensorToDevice("missing", output.data(), 8,
                                        nullptr).ok());
    Require(!loader->LoadTensorToDevice("w", nullptr, 8, nullptr).ok());
    Require(calls == before && output == std::vector<uint8_t>(8, 0));
    ++cases;
    fail_copy = true;
    Require(!loader->LoadTensorToDevice("w", output.data(), 8, nullptr).ok());
    Require(pending.empty());
    CheckBytes(output);  // Even a failed call may have queued a read.
    fail_copy = false;
    ++cases;
    failed_waits = 1;
    Require(!loader->LoadTensorToDevice("w", output.data(), 8, nullptr).ok());
    Require(pending.empty());  // Destructor retry drained, status stays failed.
    ++cases;
    {
      q4t::io::PendingHostStorage<std::vector<uint8_t>> owner(nullptr, bytes);
      for (int round = 0; round < 3; ++round) {
        owner.MarkPending();
        Cuda(__wrap_cudaMemcpyAsync(output.data(), owner.Get().data(), 8,
                                     cudaMemcpyHostToDevice, nullptr));
        Require(owner.Drain().ok());
        CheckBytes(output);
      }
      owner.MarkPending();
      Cuda(__wrap_cudaMemcpyAsync(output.data(), owner.Get().data(), 8,
                                   cudaMemcpyHostToDevice, nullptr));
      // Simulate a loader read failure/early return with outstanding upload.
    }
    Require(pending.empty());
    CheckBytes(output);
    ++cases;
    std::vector<uint8_t>* retained = nullptr;
    {
      q4t::io::PendingHostStorage<std::vector<uint8_t>> owner(nullptr, bytes);
      retained = &owner.Get();
      owner.MarkPending();
      Cuda(__wrap_cudaMemcpyAsync(output.data(), retained->data(), 8,
                                   cudaMemcpyHostToDevice, nullptr));
      failed_waits = 2;
      Require(!owner.Drain().ok());
    }
    // Failed destruction must retain the source. The mock recovers only to
    // check bytes and reclaim the intentional retention in this test.
    Require(pending.size() == 1);
    Cuda(__wrap_cudaStreamSynchronize(nullptr));
    CheckBytes(output);
    delete retained;
    ++cases;
    std::cout << "HOST_PASS cases=" << cases << " waits=" << waits << '\n';
    if (argc == 3) {
      real_cuda = true;
      cudaStream_t stream = nullptr;
      Cuda(cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking));
      void* device = nullptr;
      Cuda(cudaMalloc(&device, 8));
      for (auto current : {cudaStream_t{}, stream}) {
        Require(loader->LoadTensorToDevice("w", device, 8, current).ok());
        Cuda(cudaStreamQuery(current));
        Cuda(cudaMemcpy(output.data(), device, 8, cudaMemcpyDeviceToHost));
        CheckBytes(output);
      }
      Cuda(cudaFree(device));
      Cuda(cudaStreamDestroy(stream));
      std::cout << "GPU_PASS streams=2 bytes=16\n";
    }
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "FAIL: " << error.what() << '\n';
    return 1;
  }
}
