// Identical fixture for parent/candidate binaries; no runtime test hooks.
#include "q4t/model/model_owner.h"
#include <cuda_runtime.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <vector>

namespace fs = std::filesystem;
using namespace q4t::model;
void Check(cudaError_t e) {
  if (e != cudaSuccess) throw std::runtime_error(cudaGetErrorString(e));
}
void Check(const q4t::Status& s) {
  if (!s.ok()) throw std::runtime_error(s.message());
}
void Save(const fs::path& path, const void* data, size_t bytes) {
  std::ofstream out(path, std::ios::binary);
  out.write(static_cast<const char*>(data), bytes);
  if (!out) throw std::runtime_error("output write");
}
void Device(const fs::path& path, const void* data, size_t bytes) {
  std::vector<char> host(bytes);
  Check(cudaMemcpy(host.data(), data, bytes, cudaMemcpyDeviceToHost));
  Save(path, host.data(), bytes);
}
int main(int argc, char** argv) {
  try {
    if (argc != 2 || fs::exists(argv[1]))
      throw std::runtime_error("usage: model_replay NEW_OUTPUT");
    const fs::path output = argv[1];
    fs::create_directories(output);
    ModelConfig cfg;
    cfg.model_dir = "/home/rm01/models/dev/llm/garnermccloud/"
                    "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
    cfg.index_path = cfg.model_dir + "/model.safetensors.index.json";
    cfg.ple_sidecar = cfg.model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
    cfg.num_layers = 4; cfg.max_seq = 4; cfg.max_prefill = 8;
    cfg.max_len = 32; cfg.ple_capacity_tokens = 8;
    ModelOwner owner;
    Check(owner.Load(cfg, nullptr));
    auto& m = owner.Get();
    uint16_t* logits = nullptr;
    Check(cudaMalloc(&logits, size_t(4) * cfg.vocab * 2));
    ModelSequence seqs[4];
    for (int slot = 0; slot < 4; ++slot) {
      const int32_t prompt[4] = {846, 25, 1203, 321 + slot};
      Check(ModelBeginSequence(m, &seqs[slot], nullptr, slot));
      Check(ModelPrefill(m, &seqs[slot], prompt, 4, logits, nullptr,
                        nullptr, nullptr, slot, LogitsRows::kLastRow));
    }
    const int order[4] = {2, 0, 3, 1};
    const int counts[4] = {4, 2, 1, 1};
    for (int step = 0; step < 4; ++step) {
      if (step == 3) {
        Check(ModelEndSequence(&seqs[2]));
        Check(ModelBeginSequence(m, &seqs[2], nullptr, 2));
        const int32_t prompt[4] = {9001, 55, 700, 42};
        Check(ModelPrefill(m, &seqs[2], prompt, 4, logits, nullptr,
                          nullptr, nullptr, 2, LogitsRows::kLastRow));
      }
      const int rows = counts[step];
      std::vector<int32_t> ids(rows), history;
      std::vector<int> pos(rows), slots(rows);
      std::vector<ModelSequence*> pending;
      for (int row = 0; row < rows; ++row) {
        slots[row] = order[row];
        auto& seq = seqs[slots[row]];
        ids[row] = 42 + step * 7 + row;
        pos[row] = seq.position;
        const int width = m.ple_hash.ngram_size - 1;
        history.insert(history.end(), seq.history.end() - width, seq.history.end());
        m.rope_delta[slots[row]] = row + step;
        Check(seq.Submit({&ids[row], 1}, ModelSequence::Stage::kDecode, cfg.max_len));
        pending.push_back(&seq);
      }
      const auto submitted = ModelDecodeBatchMulti(
          m, ids.data(), pos.data(), slots.data(), history.data(), rows,
          logits, nullptr);
      Check(CompleteSequenceWork(pending, submitted, [] {
        const auto e = cudaStreamSynchronize(nullptr);
        return e == cudaSuccess ? q4t::Status()
            : q4t::Status::Fail(cudaGetErrorString(e));
      }));
      const auto dir = output / std::to_string(step);
      fs::create_directory(dir);
      Device(dir / "logits", logits, size_t(rows) * cfg.vocab * 2);
      Device(dir / "rope", m.d_rope_pos, size_t(4) * 3 * cfg.max_len * 4);
      ModelStateSnapshot state;
      Check(ModelSnapshotState(m, &state, nullptr));
      Save(dir / "recurrent", state.data.data(), state.bytes);
      const auto& full = m.layers[3];
      const size_t kv = size_t(4) * cfg.max_len * full.full.nkv * 2 * full.full.hd * 2;
      const size_t idx = size_t(4) * cfg.max_len * full.full.idx_head_dim * 2;
      Device(dir / "kv", full.kv_cache, kv);
      Device(dir / "idx_raw", full.idx_raw, idx);
      Device(dir / "idx_comp", full.idx_comp, idx);
      std::ofstream cursors(dir / "host.txt");
      for (auto& seq : seqs) {
        cursors << seq.seq_id << ' ' << seq.position << ' ' << seq.HasPending();
        for (auto id : seq.history) cursors << ' ' << id;
        cursors << '\n';
      }
    }
    Check(cudaFree(logits));
    std::cout << "PASS four-layer fixture: five prefills, four packed decodes\n";
  } catch (const std::exception& e) {
    std::cerr << "FAIL " << e.what() << '\n'; return 1;
  }
}
