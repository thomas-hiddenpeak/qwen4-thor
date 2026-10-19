// Standalone fixed-layer A1 diagnostic. All GPU execution requires the lease.
#include "q4t/io/json.h"
#include "q4t/model/moe_replay_gpu.h"
#include "q4t/model/moe_replay_io.h"
#include "q4t/quant/moe_gemm.h"
#include "q4t/trace/sha256.h"

#include <bit>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <stdexcept>

namespace {
using namespace q4t;
using namespace q4t::model;
namespace fs = std::filesystem;
constexpr int kE = 512, kHs = 2560, kIs = 640, kTopK = 10, kLayer = 2;
constexpr size_t kGemmBytes = 32 * 1024 * 1024;

void Check(bool ok, const std::string& message) {
  if (!ok) throw std::runtime_error(message);
}
void Check(const Status& status) { Check(status.ok(), status.message()); }
void Cuda(cudaError_t error) {
  Check(error == cudaSuccess, cudaGetErrorString(error));
}
std::string ReadText(const fs::path& path) {
  std::ifstream input(path);
  Check(bool(input), "open " + path.string());
  return {std::istreambuf_iterator<char>(input), {}};
}
trace::Digest HashText(const std::string& text) {
  return trace::Sha256({reinterpret_cast<const uint8_t*>(text.data()),
                         text.size()});
}
template <class T> trace::Digest Hash(const std::vector<T>& values) {
  return trace::Sha256({reinterpret_cast<const uint8_t*>(values.data()),
                         values.size() * sizeof(T)});
}
template <class T> void Save(const fs::path& path, const std::vector<T>& data) {
  std::ofstream output(path, std::ios::binary);
  output.write(reinterpret_cast<const char*>(data.data()), data.size() * sizeof(T));
  output.close();
  Check(bool(output), "write " + path.string());
}
template <class T> bool Bits(const std::vector<T>& a, const std::vector<T>& b) {
  return a.size() == b.size() &&
         std::memcmp(a.data(), b.data(), a.size() * sizeof(T)) == 0;
}

struct Fixture {
  std::vector<int32_t> tokens;
  std::vector<uint16_t> hidden;
  explicit Fixture(const fs::path& path) {
    io::Json json;
    io::JsonParseLimits limits;
    limits.reject_duplicate_keys = true;
    Check(io::ParseJson(ReadText(path), &json, false, limits));
    Check(json.IsObject() && json.object.size() == 6 &&
          json.GetString("kind") == "synthetic_layer_boundary_v1" &&
          json.GetInt("layer", -1) == kLayer &&
          json.GetInt("rows", -1) == 4 &&
          json.GetInt("hidden_size", -1) == kHs &&
          json.GetInt("seed", -1) == 777, "unsupported fixed fixture");
    const auto* array = json.GetArray("tokens");
    Check(array && array->array.size() == 4, "fixture tokens");
    for (const auto& item : array->array) {
      const auto token = item.AsInt(-1);
      Check(token >= 0 && token < 150016, "fixture token out of range");
      tokens.push_back(token);
    }
    // Integer-only generation, stable across host standard library versions.
    uint32_t state = 777;
    hidden.resize(4 * kHs);
    for (auto& value : hidden) {
      state = state * 1664525u + 1013904223u;
      value = uint16_t(0x3e00u + (state & 511u)) |
              uint16_t((state >> 16) & 0x8000u);
    }
  }
};

struct Device {
  cudaStream_t stream = nullptr;
  std::vector<void*> allocations;
  quant::MoEWeightLayout routed;
  MoEExtraWeights extra;
  Device() { Cuda(cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking)); }
  ~Device() {
    cudaStreamSynchronize(stream);
    for (void* pointer : allocations) cudaFree(pointer);
    extra.Free();
    for (void* pointer : {static_cast<void*>(routed.gu_packed),
                         static_cast<void*>(routed.gu_sf),
                         static_cast<void*>(routed.dn_packed),
                         static_cast<void*>(routed.dn_sf),
                         static_cast<void*>(routed.gu_w_scale2),
                         static_cast<void*>(routed.gu_input_scale),
                         static_cast<void*>(routed.dn_w_scale2),
                         static_cast<void*>(routed.dn_input_scale)}) {
      cudaFree(pointer);
    }
    cudaStreamDestroy(stream);
  }
  void* Alloc(size_t bytes) {
    void* pointer = nullptr;
    Cuda(cudaMalloc(&pointer, bytes));
    allocations.push_back(pointer);
    return pointer;
  }
};

trace::Digest WeightIdentity(const io::WeightIndex& index,
                             const io::WeightLoader& loader,
                             const fs::path& output) {
  std::vector<std::string> names;
  const std::string prefix = "model.language_model.layers.2.mlp.";
  for (const auto& group : index.ShardGroups()) {
    for (const auto& name : group.second) {
      if (name.starts_with(prefix)) names.push_back(name);
    }
  }
  std::sort(names.begin(), names.end());
  Check(!names.empty(), "no layer weights");
  std::string manifest;
  for (const auto& name : names) {
    const auto* tensor = loader.FindTensor(name);
    Check(tensor && tensor->byte_size() > 0, "missing tensor " + name);
    std::vector<uint8_t> bytes(tensor->byte_size());
    Check(loader.ReadTensor(name, bytes.data()));
    manifest += name + " " + std::to_string(static_cast<int>(tensor->dtype));
    for (const auto dimension : tensor->shape) {
      manifest += " " + std::to_string(dimension);
    }
    manifest += " sha256=" + trace::HexDigest(Hash(bytes)) + "\n";
  }
  std::ofstream file(output / "weights-manifest.txt");
  file << manifest;
  file.close();
  Check(bool(file), "weight manifest write");
  std::cout << "weights tensors=" << names.size() << " sha256="
            << trace::HexDigest(HashText(manifest)) << std::endl;
  return HashText(manifest);
}

struct Result {
  MoEReplayObservation route;
  std::vector<uint16_t> output;
};
void SaveResult(const fs::path& directory, const std::string& tag,
                const Result& result) {
  Save(directory / (tag + ".logits.bf16"), result.route.logits);
  Save(directory / (tag + ".ids.i32"), result.route.ids);
  Save(directory / (tag + ".weights.f32"), result.route.weights);
  Save(directory / (tag + ".output.bf16"), result.output);
}
void Equal(const Result& a, const Result& b, const std::string& tag) {
  Check(Bits(a.route.logits, b.route.logits), tag + " logits differ");
  Check(Bits(a.route.ids, b.route.ids), tag + " IDs differ");
  Check(Bits(a.route.weights, b.route.weights), tag + " weights differ");
  Check(Bits(a.output, b.output), tag + " output differs");
  std::cout << tag << " BITEXACT_PASS" << std::endl;
}

int Run(int argc, char** argv) {
  Check(argc == 5 || argc == 6 || argc == 8,
        "usage: q4t_moe_replay suite MODEL FIXTURE OUTPUT | "
        "replay MODEL FIXTURE OUTPUT ROUTE [SOURCE_SHA REASON]");
  const std::string mode = argv[1];
  Check((mode == "suite" && argc == 5) ||
        (mode == "replay" && (argc == 6 || argc == 8)), "invalid mode/args");
  const fs::path model = fs::canonical(argv[2]);
  const fs::path fixture_path = fs::canonical(argv[3]);
  const fs::path directory = fs::weakly_canonical(argv[4]);
  const fs::path relative = directory.lexically_relative(fs::current_path());
  Check(!relative.empty() && (*relative.begin() == ".q4t-work" ||
                              *relative.begin() == "build"),
        "output must be under this worktree build/ or .q4t-work/");
  Check(!fs::exists(directory), "output exists; preserve prior evidence");
  Fixture fixture(fixture_path);
  MoEReplayRecord supplied;
  if (mode == "replay") Check(ReadMoEReplay(argv[5], &supplied));
  fs::create_directories(directory);
  fs::copy_file(fixture_path, directory / "fixture.json");
  Save(directory / "fixture.hidden.bf16", fixture.hidden);
  Save(directory / "fixture.tokens.i32", fixture.tokens);
  for (const auto* variable : {"Q4T_FP8_PROJ", "Q4T_FP8_SHARED",
                                "Q4T_FP8_ATTN", "Q4T_FP8_GDN",
                                "Q4T_FP8_LMHEAD", "Q4T_FP8_HC",
                                "Q4T_FP8_HC_UP",
                                "Q4T_MOE_DUMP", "Q4T_MOE_CHUNK_ORDER",
                                "Q4T_MOE_PARTITION"}) {
    Check(std::getenv(variable) == nullptr,
          std::string("unset experimental variable ") + variable);
  }
  MoEReplayIdentity base;
  base.layer = kLayer;
  base.experts = kE;
  base.top_k = kTopK;
  Check(trace::HashFile("/proc/self/exe", &base.binary));
  base.model = HashText(ReadText(model / "config.json") + "\n" +
                       ReadText(model / "model.safetensors.index.json"));
  io::WeightIndex* index_raw = nullptr;
  Check(io::WeightIndex::Open((model / "model.safetensors.index.json").string(),
                              &index_raw));
  std::unique_ptr<io::WeightIndex> index(index_raw);
  io::WeightLoader* loader_raw = nullptr;
  Check(io::WeightLoader::Create(model.string(), *index, 16, &loader_raw));
  std::unique_ptr<io::WeightLoader> loader(loader_raw);
  base.weights = WeightIdentity(*index, *loader, directory);
  std::cout << "execution_binary=" << trace::HexDigest(base.binary)
            << " model=" << trace::HexDigest(base.model) << std::endl;
  Device device;
  Check(quant::LoadMoEWeights(*loader, kLayer, kE, kHs, kIs,
                              &device.routed, device.stream));
  Check(LoadMoEExtra(*loader, "model.language_model.layers.2.mlp", kE, kHs,
                     kIs, &device.extra, device.stream));
  const size_t workspace_bytes = MoEForwardWorkspaceBytes(4, kTopK, kHs,
                                                          kIs, kIs, kE);
  void* workspace = device.Alloc(workspace_bytes);
  void* gemm = device.Alloc(kGemmBytes);
  auto* x = static_cast<uint16_t*>(device.Alloc(4 * kHs * 2));
  auto* y = static_cast<uint16_t*>(device.Alloc(4 * kHs * 2));
  const auto identity = [&](int row, int rows) {
    Check(row >= 0 && rows > 0 && row + rows <= 4, "fixture slice");
    auto id = base;
    id.position = row;
    id.rows = rows;
    id.tokens.assign(fixture.tokens.begin(), fixture.tokens.begin() + row + rows);
    const std::vector<uint16_t> hidden(fixture.hidden.begin() + row * kHs,
                                       fixture.hidden.begin() + (row + rows) * kHs);
    id.hidden = Hash(hidden);
    return id;
  };
  const auto run = [&](const MoEReplayIdentity& id, const MoEReplayRecord* record,
                       bool production,
                       const MoEReplayCompatibility* compatibility = nullptr) {
    const int rows = id.rows;
    Cuda(cudaMemcpyAsync(x, fixture.hidden.data() + id.position * kHs,
                          rows * kHs * 2, cudaMemcpyHostToDevice, device.stream));
    Result result;
    struct Completion {
      cudaStream_t stream;
      ~Completion() { cudaStreamSynchronize(stream); }
    } completion{device.stream};
    if (production) {
      Check(MoEForward(x, device.routed, device.extra, y, rows, kTopK,
                        workspace, workspace_bytes, gemm, kGemmBytes,
                        device.stream));
      const size_t bytes = quant::MoEWorkspace::RequiredBytes(rows, kTopK,
                                                            kHs, kIs);
      const auto* scratch = static_cast<const uint8_t*>(workspace) +
                             ((bytes + 7) & ~size_t(7));
      result.route.logits.resize(rows * kE);
      result.route.ids.resize(rows * kTopK);
      result.route.weights.resize(rows * kTopK);
      Cuda(cudaMemcpyAsync(result.route.logits.data(), scratch, rows * kE * 2,
                            cudaMemcpyDeviceToHost, device.stream));
      scratch += rows * kE * 2;
      Cuda(cudaMemcpyAsync(result.route.ids.data(), scratch, rows * kTopK * 4,
                            cudaMemcpyDeviceToHost, device.stream));
      scratch += rows * kTopK * 4;
      Cuda(cudaMemcpyAsync(result.route.weights.data(), scratch, rows * kTopK * 4,
                            cudaMemcpyDeviceToHost, device.stream));
    } else {
      Check(MoEReplayForward(x, device.routed, device.extra, y, workspace,
                              workspace_bytes, gemm, kGemmBytes, device.stream,
                              id, record, &result.route, compatibility));
    }
    result.output.resize(rows * kHs);
    Cuda(cudaMemcpyAsync(result.output.data(), y, rows * kHs * 2,
                          cudaMemcpyDeviceToHost, device.stream));
    Cuda(cudaStreamSynchronize(device.stream));
    Cuda(cudaStreamQuery(device.stream));
    return result;
  };
  if (mode == "replay") {
    const auto& source = supplied.source();
    Check(source.position <= 3 && source.rows <= 4, "source outside fixture");
    const auto id = identity(source.position, source.rows);
    MoEReplayCompatibility compatibility;
    if (argc == 8) {
      Check(trace::HexDigest(source.binary) == argv[6], "allowed source mismatch");
      compatibility = {source.binary, id.binary, argv[7]};
      Check(!compatibility.allowed_difference.empty(), "empty compatibility reason");
      std::ofstream note(directory / "compatibility.txt");
      note << "source=" << trace::HexDigest(source.binary) << "\nexecution="
           << trace::HexDigest(id.binary) << "\nallowed_difference="
           << compatibility.allowed_difference << '\n';
      note.close();
      Check(bool(note), "compatibility note write");
    }
    auto result = run(id, &supplied, false, argc == 8 ? &compatibility : nullptr);
    SaveResult(directory, "replay", result);
    std::cout << "replay PASS source_binary=" << trace::HexDigest(source.binary)
              << std::endl;
    return 0;
  }
  Check(MoEReplayRouterContract());
  Result batch;
  for (int shape = 0; shape < 5; ++shape) {
    const int row = shape == 0 ? 0 : shape - 1;
    const int rows = shape == 0 ? 4 : 1;
    const auto id = identity(row, rows);
    const std::string tag = "t" + std::to_string(rows) + "-row" + std::to_string(row);
    auto production = run(id, nullptr, true);
    auto normal = run(id, nullptr, false);
    SaveResult(directory, tag + "-production", production);
    SaveResult(directory, tag + "-normal", normal);
    Equal(production, normal, tag + " production-normal");
    MoEReplayRecord record;
    auto ids = normal.route.ids;
    Check(record.Prepare(id, id, ids));
    std::fill(ids.begin(), ids.end(), -1);  // Owner reuse before GPU submission.
    Check(WriteMoEReplay((directory / (tag + ".route")).string(), record));
    MoEReplayRecord restored;
    Check(ReadMoEReplay((directory / (tag + ".route")).string(), &restored));
    auto replay = run(id, &restored, false);
    SaveResult(directory, tag + "-replay", replay);
    Equal(normal, replay, tag + " normal-replay");
    if (shape == 0) batch = normal;
    else {
      std::vector<int32_t> fixed(batch.route.ids.begin() + row * kTopK,
                                 batch.route.ids.begin() + (row + 1) * kTopK);
      Check(record.Prepare(id, id, fixed));
      auto cross = run(id, &record, false);
      SaveResult(directory, tag + "-batch-ids", cross);
      size_t id_diff = 0, weight_diff = 0, output_diff = 0;
      for (int i = 0; i < kTopK; ++i) {
        id_diff += normal.route.ids[i] != fixed[i];
        weight_diff += std::bit_cast<uint32_t>(cross.route.weights[i]) !=
            std::bit_cast<uint32_t>(batch.route.weights[row * kTopK + i]);
      }
      for (int i = 0; i < kHs; ++i) {
        output_diff += cross.output[i] != batch.output[row * kHs + i];
      }
      std::cout << "cross_shape row=" << row << " normal_id_diff=" << id_diff
                << " fixed_weight_diff=" << weight_diff
                << " fixed_output_diff=" << output_diff << std::endl;
    }
  }
  // Invalid records/actual hidden are rejected before any output write. Then
  // prove the same stream and scratch can be reused after each rejection.
  const auto id = identity(0, 1);
  const auto reference = run(id, nullptr, false);
  for (int failure = 0; failure < 4; ++failure) {
    auto wrong = id;
    if (failure == 0) wrong.model[0] ^= 1;
    if (failure == 1) wrong.layer++;
    if (failure == 2) wrong.tokens[0]++;
    if (failure == 3) wrong.hidden[0] ^= 1;
    MoEReplayRecord record;
    Check(record.Prepare(wrong, wrong, reference.route.ids));
    Cuda(cudaMemsetAsync(y, 0xa5, kHs * 2, device.stream));
    MoEReplayObservation observation;
    const auto status = MoEReplayForward(
        x, device.routed, device.extra, y, workspace, workspace_bytes, gemm,
        kGemmBytes, device.stream, failure == 3 ? wrong : id, &record,
        &observation);
    Check(!status.ok(), "invalid execution was accepted");
    Cuda(cudaStreamQuery(device.stream));
    std::vector<uint16_t> sentinel(kHs);
    Cuda(cudaMemcpy(sentinel.data(), y, kHs * 2, cudaMemcpyDeviceToHost));
    Check(std::all_of(sentinel.begin(), sentinel.end(),
                      [](uint16_t value) { return value == 0xa5a5; }),
          "rejected execution wrote output");
    std::cout << "gpu_rejection " << failure << " PASS " << status.message()
              << std::endl;
  }
  Cuda(cudaMemsetAsync(workspace, 0xa5, workspace_bytes, device.stream));
  MoEReplayRecord recovery;
  Check(recovery.Prepare(id, id, reference.route.ids));
  Equal(reference, run(id, &recovery, false), "post-error-buffer-reuse");
  std::cout << "suite PASS same_shape=5 production_control=5 cross_shape=4\n";
  return 0;
}
}  // namespace

int main(int argc, char** argv) {
  try {
    return Run(argc, argv);
  } catch (const std::exception& error) {
    std::cerr << "FAIL: " << error.what() << '\n';
    return 1;
  }
}
