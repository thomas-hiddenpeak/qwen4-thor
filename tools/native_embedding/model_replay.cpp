// Targeted native embedding differential driver, not a performance benchmark.
#include <cuda_runtime.h>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>

#include "q4t/model/embedding_model.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/embedding_assets.h"
#include "q4t/server/lifetime.h"
#include "../../src/server/cuda_memory_backend.h"

namespace {
using namespace q4t;
namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;

void Require(bool ok, const std::string& error) {
  if (!ok) throw std::runtime_error(error);
}
void Check(const model::EmbeddingComputeResult& result) {
  Require(result.ok(), result.status.message());
}
void Save(const fs::path& path, const uint16_t* device, size_t count) {
  std::vector<uint16_t> values(count);
  Require(cudaMemcpy(values.data(), device, count * 2,
                      cudaMemcpyDeviceToHost) == cudaSuccess, "snapshot copy");
  std::ofstream file(path, std::ios::binary);
  file.write(reinterpret_cast<const char*>(values.data()), count * 2);
  Require(file.good(), "snapshot write");
}

int Replay(const fs::path& directory, const fs::path& input,
            const fs::path& output) {
  const char* lease = std::getenv("Q4T_MANAGED_LEASE_FD");
  Require(lease && *lease, "run inside gpu_exclusive.py --inherit-lease");
  char* end = nullptr;
  const long fd = std::strtol(lease, &end, 10);
  struct stat lock_stat {};
  Require(end && !*end && fd >= 0 && fd < 1048576 &&
              !fstat(fd, &lock_stat) && S_ISREG(lock_stat.st_mode) &&
              !flock(fd, LOCK_EX | LOCK_NB), "inherited GPU lease unavailable");
  Require(fs::is_regular_file(input) && fs::file_size(input) <= 1024 * 1024,
           "invalid replay plan file");
  std::ifstream file(input);
  std::string text((std::istreambuf_iterator<char>(file)), {});
  io::Json plan;
  Require(io::ParseJson(text, &plan, false,
                        {.reject_duplicate_keys = true}).ok(), "plan JSON");
  const auto* cases = plan.GetArray("cases");
  Require(cases && !cases->array.empty() && cases->array.size() <= 32,
           "replay case budget");
  Require(!fs::exists(output) && fs::create_directories(output),
           "fresh output directory required");
  // File page cache, parent staging, GPU weights, maximum workspace and
  // 1GiB spare are all unified DRAM. This is admission, not a physical cap.
  Require(runtime::ReadSystemMemory().CanAllocate(
      3 * server::kNativeEmbeddingFileBytes + (1536ULL << 20)),
      "insufficient observed unified memory for optional model acceptance");
  server::EmbeddingHostAssets assets;
  server::GrammarControl control;
  control.compile_deadline = Clock::now() + std::chrono::seconds(60);
  auto status = server::LoadEmbeddingAssets(directory, &assets, control);
  Require(status.ok(), status.message());
  uint8_t* weights = nullptr;
  uint8_t* workspace = nullptr;
  server::ScopeExit release([&] {
    if (workspace) (void)server::detail::ReleaseCudaMemory(
        workspace, "embedding-replay.workspace");
    if (weights) (void)server::detail::ReleaseCudaMemory(
        weights, "embedding-replay.weights");
  });
  auto allocated = server::detail::AllocateCudaMemory(
      weights, assets.layout.bytes, "embedding-replay.weights");
  Require(allocated.ok(), allocated.Reason());
  Require(cudaMemcpy(weights, assets.WeightBytes().data(), assets.layout.bytes,
                      cudaMemcpyHostToDevice) == cudaSuccess, "weight copy");
  model::EmbeddingDeviceWeights binding;
  status = model::BindEmbeddingDeviceWeights(weights, assets.layout.bytes,
                                              assets.layout, &binding);
  Require(status.ok(), status.message());
  std::vector<uint8_t>().swap(assets.file_bytes);
  size_t index = 0;
  for (const auto& row : cases->array) {
    std::vector<uint32_t> actual;
    const auto* tokens = row.GetArray("ids");
    Require(tokens && !tokens->array.empty() && tokens->array.size() <= 8192,
             "case token IDs missing or excessive");
    std::vector<int32_t> ids;
    for (const auto& item : tokens->array) {
      const auto id = item.AsInt(-1);
      Require(id >= 0 && id < model::EmbeddingConfig::kVocab, "invalid token");
      ids.push_back(id);
    }
    if (const auto* source = row.Find("text")) {
      Require(source->IsString() && assets.tokenizer->Encode(source->str,
               &actual).ok(), "native tokenization failed");
      Require(std::vector<int32_t>(actual.begin(), actual.end()) == ids,
               "native tokenizer IDs differ from independent reference");
    }
    model::EmbeddingWorkspaceLayout layout;
    Require(model::PlanEmbeddingWorkspace(ids.size(), &layout).ok(),
             "workspace plan");
    allocated = server::detail::AllocateCudaMemory(
        workspace, layout.bytes, "embedding-replay.workspace");
    Require(allocated.ok(), allocated.Reason());
    const fs::path case_output = output / ("case-" + std::to_string(index));
    Require(fs::create_directory(case_output), "case output");
    model::EmbeddingRun run;
    Check(run.Begin(binding, ids, workspace, layout.bytes));
    auto* hidden = reinterpret_cast<uint16_t*>(workspace + layout.hidden);
    const bool trace = row.GetBool("trace_layers");
    if (trace) Save(case_output / "layer-00.bf16", hidden,
                     ids.size() * model::EmbeddingConfig::kHidden);
    for (int layer = 0; layer < model::EmbeddingConfig::kLayers; ++layer) {
      Check(run.NextLayer());
      Require(run.completed_layers() == layer + 1, "completion state");
      if (layer == 0 && row.GetBool("trace_primitives")) {
        const size_t t = ids.size(), p = layout.padded_tokens;
        const auto dump = [&](const char* name, size_t offset, size_t count) {
          Save(case_output / (std::string(name) + ".bf16"),
               reinterpret_cast<const uint16_t*>(workspace + offset), count);
        };
        dump("q", layout.q, t * 2048);
        dump("k", layout.k, t * 1024);
        dump("v", layout.v, t * 1024);
        dump("head_q", layout.head_q, p * 16 * 128);
        dump("head_k", layout.head_k, p * 8 * 128);
        dump("transposed_v", layout.transposed_v, p * 8 * 128);
        dump("rope", layout.rope, t * 128);
        dump("scores", layout.scores, t * p);
        dump("head_output", layout.head_output, t * 128);
        dump("attention", layout.attention, t * 2048);
        dump("post_norm", layout.normalized, t * 1024);
        dump("gate", layout.gate, t * 3072);
        dump("up", layout.up, t * 3072);
        dump("projected", layout.projected, t * 1024);
      }
      if (trace) {
        const std::string number = (layer + 1 < 10 ? "0" : "") +
                                   std::to_string(layer + 1);
        Save(case_output / ("layer-" + number + ".bf16"), hidden,
              ids.size() * model::EmbeddingConfig::kHidden);
      }
    }
    std::vector<uint16_t> last(model::EmbeddingConfig::kHidden);
    Check(run.Finish(last));
    Require(run.finished() && !run.NextLayer().ok() && !run.Finish(last).ok(),
             "completed request must not execute again");
    std::ofstream target(case_output / "last.bf16", std::ios::binary);
    target.write(reinterpret_cast<const char*>(last.data()), last.size() * 2);
    Require(target.good(), "output write");
    auto freed = server::detail::ReleaseCudaMemory(
        workspace, "embedding-replay.workspace");
    Require(freed.ok(), freed.Reason());
    std::cout << "case=" << index++ << " tokens=" << ids.size()
              << " workspace_bytes=" << layout.bytes << " layers=28\n"
              << std::flush;
  }
  const auto freed = server::detail::ReleaseCudaMemory(
      weights, "embedding-replay.weights");
  Require(freed.ok(), freed.Reason());
  std::cout << "cases=" << index << " native_buffers_released=true\n";
  return 0;
}
}  // namespace

int main(int argc, char** argv) {
  const int worker = q4t::server::MaybeRunEmbeddingAssetWorker(argc, argv);
  if (worker >= 0) return worker;
  if (argc != 4) {
    std::cerr << "usage: q4t_embedding_replay ASSET_DIR PLAN_JSON NEW_OUTPUT\n";
    return 2;
  }
  try {
    return Replay(argv[1], argv[2], argv[3]);
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
