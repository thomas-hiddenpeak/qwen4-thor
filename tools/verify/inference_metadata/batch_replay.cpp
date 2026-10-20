// Same source for exact parent/candidate; checks layout independently as well.
#include "q4t/model/model_owner.h"
#include "q4t/mtp/mtp.h"

#include <cuda_runtime.h>

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <vector>

namespace fs = std::filesystem;
using namespace q4t::model;

void Check(cudaError_t error) {
  if (error != cudaSuccess) throw std::runtime_error(cudaGetErrorString(error));
}
void Check(const q4t::Status& status) {
  if (!status.ok()) throw std::runtime_error(status.message());
}
void Require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}
void Save(const fs::path& path, const void* data, size_t bytes) {
  std::ofstream out(path, std::ios::binary);
  out.write(static_cast<const char*>(data), bytes);
  Require(bool(out), "output write failed");
}
void Device(const fs::path& path, const void* data, size_t bytes) {
  std::vector<char> host(bytes);
  Check(cudaMemcpy(host.data(), data, bytes, cudaMemcpyDeviceToHost));
  Save(path, host.data(), bytes);
}
void Ints(const int* device, const std::vector<int>& expected) {
  std::vector<int> actual(expected.size());
  Check(cudaMemcpy(actual.data(), device, actual.size() * sizeof(int),
                   cudaMemcpyDeviceToHost));
  Require(actual == expected, "independent metadata layout mismatch");
}
void Snapshot(const fs::path& dir, const Model& m, const uint16_t* logits,
              int rows) {
  Check(cudaStreamSynchronize(nullptr));
  fs::create_directory(dir);
  Device(dir / "logits", logits, size_t(rows) * m.cfg.vocab * 2);
  Device(dir / "rope", m.d_rope_pos,
         size_t(m.cfg.max_seq) * 3 * m.cfg.max_len * sizeof(int));
  Save(dir / "delta", m.rope_delta.data(), m.rope_delta.size() * sizeof(int));
  ModelStateSnapshot state;
  Check(ModelSnapshotState(m, &state, nullptr));
  Save(dir / "recurrent", state.data.data(), state.bytes);
  const auto& full = m.layers[3];
  const size_t kv = size_t(m.cfg.max_seq) * m.cfg.max_len * full.full.nkv *
                    2 * full.full.hd * 2;
  const size_t index = size_t(m.cfg.max_seq) * m.cfg.max_len *
                       full.full.idx_head_dim * 2;
  Device(dir / "kv", full.kv_cache, kv);
  Device(dir / "idx_raw", full.idx_raw, index);
  Device(dir / "idx_comp", full.idx_comp, index);
  std::cout << "saved " << dir.filename().string() << '\n' << std::flush;
}

int main(int argc, char** argv) {
  try {
    Require(argc == 2 && !fs::exists(argv[1]), "usage: replay NEW_OUTPUT");
    const fs::path output = argv[1];
    fs::create_directories(output);
    ModelConfig cfg;
    cfg.model_dir = "/home/rm01/models/dev/llm/garnermccloud/"
                    "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
    cfg.index_path = cfg.model_dir + "/model.safetensors.index.json";
    cfg.ple_sidecar = cfg.model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
    cfg.num_layers = 4;
    cfg.max_seq = 4;
    cfg.max_prefill = 8;
    cfg.max_len = 32;
    cfg.ple_capacity_tokens = 8;
    ModelOwner owner;
    Check(owner.Load(cfg, nullptr));
    auto& m = owner.Get();
    ModelSequence slots[4];
    for (int slot = 0; slot < 4; ++slot)
      Check(ModelBeginSequence(m, &slots[slot], nullptr, slot));
    Check(cudaMemset(m.d_rope_pos, 0, size_t(4) * 3 * 32 * sizeof(int)));
    uint16_t* logits = nullptr;
    uint16_t* trunk = nullptr;
    uint16_t* vision_data = nullptr;
    Check(cudaMalloc(&logits, size_t(8) * cfg.vocab * 2));
    Check(cudaMalloc(&trunk, size_t(8) * m.hc_dim() * 2));
    Check(cudaMalloc(&vision_data, size_t(4) * cfg.hs * 2));
    const int32_t prompt[] = {846, 25, 1203, 321};
    Check(ModelForward(m, prompt, 4, logits, nullptr));
    Ints(m.d_ids, {846, 25, 1203, 321});
    Ints(m.d_positions, {0, 1, 2, 3});
    Snapshot(output / "01-prefill", m, logits, 4);
    m.rope_delta[0] = 2;
    Check(ModelDecodeStep(m, 97, 4, prompt, logits, nullptr));
    Ints(m.d_ids, {97});
    Ints(m.d_positions, {4});
    for (int axis = 0; axis < 3; ++axis)
      Ints(m.d_rope_pos + axis * cfg.max_len + 4, {6});
    Snapshot(output / "02-step", m, logits, 1);
    const int32_t history[] = {846, 25, 1203, 321, 97};
    const int32_t next[] = {131, 211, 313};
    Check(ModelDecodeBatch(m, next, 3, 5, history, 5, logits, nullptr));
    Ints(m.d_ids, {131, 211, 313});
    Ints(m.d_positions, {5, 6, 7});
    for (int axis = 0; axis < 3; ++axis)
      Ints(m.d_rope_pos + axis * cfg.max_len + 5, {7, 8, 9});
    Snapshot(output / "03-batch", m, logits, 3);
    const int32_t ragged_ids[] = {42, 43, 52, 53, 54};
    const int lengths[] = {2, 3}, ragged_slots[] = {1, 2};
    Check(ModelPrefillBatch(m, ragged_ids, lengths, ragged_slots, 2,
                            logits, nullptr));
    Ints(m.d_ids, {42, 43, 52, 53, 54});
    Ints(m.d_positions, {0, 1, 0, 1, 2});
    Ints(m.d_token_seq_id, {1, 1, 2, 2, 2});
    Ints(m.d_ragged_seq_offset, {0, 2, 5});
    Ints(m.d_ragged_token_local, {0, 1, 0, 1, 2});
    Snapshot(output / "04-ragged", m, logits, 5);
    const int32_t mixed_ids[] = {44, 62, 63, 64};
    const int mixed_lengths[] = {1, 3}, mixed_slots[] = {1, 3};
    const int base[] = {2, 0};
    Require(cfg.eos_token_id >= 0 && cfg.eos_token_id < cfg.vocab,
            "fixture EOS outside vocab");
    const auto eos = static_cast<int32_t>(cfg.eos_token_id);
    const int32_t prior[] = {42, 43, eos, eos};
    Require(m.ple_hash.ngram_size == 3, "fixture requires history width 2");
    Check(ModelMixedBatch(m, mixed_ids, mixed_lengths, mixed_slots, base,
                          prior, 2, logits, nullptr));
    Ints(m.d_ids, {44, 62, 63, 64});
    Ints(m.d_positions, {2, 0, 1, 2});
    Ints(m.d_token_seq_id, {1, 3, 3, 3});
    Ints(m.d_ragged_seq_offset, {0, 1, 4});
    Ints(m.d_ragged_token_local, {0, 0, 1, 2});
    Snapshot(output / "05-mixed", m, logits, 4);
    std::vector<uint16_t> features(size_t(4) * cfg.hs);
    for (size_t i = 0; i < features.size(); ++i)
      features[i] = uint16_t(0x3e00 + i % 31);  // finite BF16 fixture values
    Check(cudaMemcpy(vision_data, features.data(), features.size() * 2,
                     cudaMemcpyHostToDevice));
    VisionFeatures vision;
    vision.device = vision_data;
    vision.num_tokens = 4;
    vision.grids = {{1, 4, 4}};
    const int32_t image_ids[] = {42, cfg.image_token_id, cfg.image_token_id,
                                cfg.image_token_id, cfg.image_token_id, 43};
    Check(ModelForward(m, image_ids, 6, logits, nullptr, &vision, 2));
    Ints(m.d_img_pos, {1, 2, 3, 4});
    Ints(m.d_positions, {0, 1, 2, 3, 4, 5});
    const int* image_rope = m.d_rope_pos + 2 * 3 * cfg.max_len;
    Ints(image_rope, {0, 1, 1, 1, 1, 3});
    Ints(image_rope + cfg.max_len, {0, 1, 1, 2, 2, 3});
    Ints(image_rope + 2 * cfg.max_len, {0, 1, 2, 1, 2, 3});
    Require(m.rope_delta[2] == -2, "image rope delta mismatch");
    Snapshot(output / "06-image", m, logits, 6);
    Device(output / "06-image/img_pos", m.d_img_pos, 4 * sizeof(int));
    Check(ModelDecodeStep(m, 44, 6, image_ids, logits, nullptr, nullptr, 2));
    for (int axis = 0; axis < 3; ++axis)
      Ints(image_rope + axis * cfg.max_len + 6, {4});
    Snapshot(output / "07-image-step", m, logits, 1);
    vision.num_tokens = 3;
    const auto rejected = ModelForward(m, image_ids, 6, logits, nullptr,
                                       &vision, 2);
    Require(!rejected.ok() && rejected.message() ==
                "vision: multimodal-token count mismatch 4 vs 3",
            "expected image-count rejection");
    Save(output / "rejection.txt", rejected.message().data(),
         rejected.message().size());
    // No test-side synchronization before reuse: the candidate's unfinished
    // batch must retain sources and drain them at the next Begin.
    Check(ModelForward(m, prompt, 4, logits, nullptr, nullptr, 2));
    Ints(m.d_ids, {846, 25, 1203, 321});
    Snapshot(output / "08-recovery", m, logits, 4);
    const int32_t draft_ids[] = {846, 25, 1203, 321, 97, 131, 211, 313};
    Check(ModelBeginSequence(m, &slots[3], nullptr, 3));
    Check(ModelPrefill(m, &slots[3], draft_ids, 8, logits, nullptr,
                      trunk, nullptr, 3));
    Snapshot(output / "09-draft-input", m, logits, 8);
    Device(output / "09-draft-input/trunk", trunk, size_t(8) * m.hc_dim() * 2);
    q4t::mtp::MtpModel draft;
    q4t::mtp::MtpConfig dcfg;
    dcfg.mtp_dir = cfg.model_dir + "/mtp";
    dcfg.max_seq = 4;
    dcfg.max_len = 32;
    dcfg.max_prefill = 3;
    Check(q4t::mtp::LoadMtp(dcfg, m.head.embed_tokens, m.head.lm_head,
                           &draft, nullptr));
    Check(q4t::mtp::MtpResetState(draft, nullptr));
    uint16_t* g = nullptr;
    Check(cudaMalloc(&g, size_t(m.hc_dim()) * 2));
    const int positions[] = {0, 1, 2, 3, 4, 5, 6, 7};
    for (int slot : {3, 1}) {
      Check(q4t::mtp::MtpResetState(draft, nullptr, slot));
      int32_t d0 = -1;
      Check(q4t::mtp::MtpDraftExtend(draft, draft_ids, trunk, positions, 8,
                                    &d0, g, nullptr, slot));
      Check(cudaStreamSynchronize(nullptr));
      const auto dir = output / ("mtp-" + std::to_string(slot));
      fs::create_directory(dir);
      Save(dir / "d0", &d0, sizeof(d0));
      Device(dir / "g", g, size_t(m.hc_dim()) * 2);
      Device(dir / "kv", draft.kv_cache, size_t(4) * draft.kv_bytes);
      Device(dir / "idx_raw", draft.idx_raw, size_t(4) * draft.idx_bytes);
      Device(dir / "idx_comp", draft.idx_comp, size_t(4) * draft.idx_bytes);
      Device(dir / "rope", draft.d_rope_pos, size_t(3) * 32 * sizeof(int));
      std::cout << "saved mtp slot " << slot << '\n' << std::flush;
    }
    Check(cudaFree(g));
    draft.Free();
    Check(cudaFree(vision_data));
    Check(cudaFree(trunk));
    Check(cudaFree(logits));
    std::cout << "PASS 9 main forwards, 1 rejection, 2 chunked draft extends\n";
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
