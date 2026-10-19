// Compile the unmodified production kernel source into this diagnostic TU.
// No checkpoint parameter is introduced: the observed RegKernel<8> stays used.
#include "../../../src/model/linear_attention.cu"
#include "q4t/trace/sha256.h"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace q4t::diagnostic::gdn {
namespace fs = std::filesystem;
using namespace q4t::model;
void Check(bool okay, const std::string& message) {
  if (!okay) throw std::runtime_error(message);
}
void Cuda(cudaError_t status) {
  Check(status == cudaSuccess, cudaGetErrorString(status));
}
template <typename T>
std::vector<T> Load(const fs::path& path, size_t count) {
  Check(fs::file_size(path) == count * sizeof(T),
        "fixture size: " + path.string());
  std::vector<T> data(count);
  std::ifstream file(path, std::ios::binary);
  file.read(reinterpret_cast<char*>(data.data()), count * sizeof(T));
  Check(bool(file), "fixture read");
  return data;
}
template <typename T>
void Save(const fs::path& path, const std::vector<T>& data) {
  std::ofstream file(path, std::ios::binary);
  file.write(reinterpret_cast<const char*>(data.data()),
             data.size() * sizeof(T));
  file.close();
  Check(bool(file), "write output");
}
struct Device {
  cudaStream_t stream{};
  std::vector<void*> buffers;
  Device() { Cuda(cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking)); }
  ~Device() {
    cudaStreamSynchronize(stream);
    for (auto* buffer : buffers) cudaFree(buffer);
    cudaStreamDestroy(stream);
  }
  template <typename T>
  T* Alloc(size_t count) {
    T* result = nullptr;
    Cuda(cudaMalloc(&result, count * sizeof(T)));
    buffers.push_back(result);
    return result;
  }
  template <typename T>
  void Upload(T* target, const std::vector<T>& data) {
    auto submitted =
        cudaMemcpyAsync(target, data.data(), data.size() * sizeof(T),
                        cudaMemcpyHostToDevice, stream);
    auto finished = cudaStreamSynchronize(stream);
    Cuda(submitted);
    Cuda(finished);
  }
  template <typename T>
  std::vector<T> Read(T* pointer, size_t count) {
    std::vector<T> data(count);
    auto submitted = cudaMemcpyAsync(data.data(), pointer, count * sizeof(T),
                                     cudaMemcpyDeviceToHost, stream);
    auto finished = cudaStreamSynchronize(stream);
    Cuda(submitted);
    Cuda(finished);
    return data;
  }
};
int Run(int argc, char** argv) {
  Check(argc == 3, "usage: gdn_replay FIXTURE NEW_OUTPUT");
  const auto input = fs::canonical(argv[1]);
  const auto output = fs::weakly_canonical(argv[2]);
  auto relative = output.lexically_relative(fs::current_path());
  Check(!relative.empty() &&
            (*relative.begin() == ".q4t-work" || *relative.begin() == "build"),
        "output path");
  Check(!fs::exists(output), "output exists");
  fs::create_directories(output);
  Device device;
  const auto stream = device.stream;
  const auto load = [&](const char* name, size_t size) {
    auto* data = device.Alloc<uint16_t>(size);
    device.Upload(data, Load<uint16_t>(input / name, size));
    return data;
  };
  auto* raw = load("qkv", 4 * 10240);
  auto* z = load("z", 4 * 6144);
  auto* a = load("a", 4 * 48);
  auto* beta = load("beta", 4 * 48);
  auto* conv_weight = load("conv1d", 10240 * 4);
  auto* norm = load("norm", 128);
  auto* alog = load("A_log", 48);
  auto* bias = load("dt_bias", 48);
  auto* qkv = device.Alloc<uint16_t>(4 * 10240);
  auto* y = device.Alloc<uint16_t>(4 * 6144);
  auto* state = device.Alloc<float>(48 * 128 * 128);
  auto* conv = device.Alloc<uint16_t>(10240 * 3);
  const auto reset = [&] {
    device.Upload(state, Load<float>(input / "ssm.initial", 48 * 128 * 128));
    device.Upload(conv, Load<uint16_t>(input / "conv.initial", 10240 * 3));
  };
  const auto run = [&](const std::string& label, int offset, int count) {
    CausalConv1dWithCkptKernel<<<dim3(40, count), 256, 0, stream>>>(
        raw + offset * 10240, qkv, conv, nullptr, conv_weight, count, 10240, 4,
        0, nullptr);
    Cuda(cudaGetLastError());
    Conv1dUpdateStateKernel<<<40, 256, 0, stream>>>(conv, raw + offset * 10240,
                                                    count, 10240, 4);
    Cuda(cudaGetLastError());
    GdnRegPrepNormKernel<<<(count * 16 + 3) / 4, 128, 0, stream>>>(
        qkv, count, 16, 128, 10240);
    Cuda(cudaGetLastError());
    Save(output / (label + ".qkv_norm"), device.Read(qkv, count * 10240));
    GatedDeltaNetRegKernel<8><<<dim3(4, 48), 128, 0, stream>>>(
        qkv, a + offset * 48, bias, alog, beta + offset * 48, state, y, count,
        16, 128, 3, 128, 10240, 48);
    Cuda(cudaGetLastError());
    Save(output / (label + ".raw_y"), device.Read(y, count * 6144));
    NormGateKernel<<<dim3(count, 48), 128, 0, stream>>>(y, z + offset * 6144,
                                                        norm, 1e-6f, 48, 128);
    Cuda(cudaGetLastError());
    Save(output / (label + ".gated"), device.Read(y, count * 6144));
    Save(output / (label + ".ssm"), device.Read(state, 48 * 128 * 128));
    Save(output / (label + ".conv"), device.Read(conv, 10240 * 3));
    std::cout << "completed " << label << '\n' << std::flush;
  };
  // Sequential captured a values; all other projected inputs are identical.
  device.Upload(a, Load<uint16_t>(input / "a_t1", 4 * 48));
  reset();
  for (int row = 0; row < 4; ++row) run("t1-" + std::to_string(row), row, 1);
  device.Upload(a, Load<uint16_t>(input / "a", 4 * 48));
  reset();
  run("t4", 0, 4);
  reset();
  run("prefix1", 0, 1);
  reset();
  run("prefix2", 0, 2);
  // Explicit intervention: replace only the known a[2,43] fork, never weights.
  auto intervention = Load<uint16_t>(input / "a", 4 * 48);
  auto target = Load<uint16_t>(input / "a_t1", 4 * 48);
  size_t differences = 0;
  for (size_t i = 0; i < target.size(); ++i) {
    if (intervention[i] == target[i]) continue;
    Check(i == 2 * 48 + 43, "unexpected a intervention location");
    ++differences;
  }
  Check(differences == 1, "missing a intervention");
  intervention[2 * 48 + 43] = target[2 * 48 + 43];
  device.Upload(a, intervention);
  reset();
  run("fixed-a", 0, 4);
  std::ofstream done(output / "completion.json");
  done << "{\"status\":\"PASS\",\"pipeline_calls\":8,\"checkpoint_calls\":0}\n";
  done.close();
  Check(bool(done), "completion write");
  return 0;
}
}  // namespace q4t::diagnostic::gdn
int main(int argc, char** argv) {
  try {
    return q4t::diagnostic::gdn::Run(argc, argv);
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
