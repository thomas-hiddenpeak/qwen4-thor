// B1a: exact host metadata versus real device readback, identical shapes.
#include "q4t/model/model.h"
#include "q4t/mtp/mtp.h"
#include "q4t/test.h"

#include <bit>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

void Check(cudaError_t error) {
  if (error != cudaSuccess) {
    throw std::runtime_error(cudaGetErrorString(error));
  }
}
void Check(const q4t::Status& status) {
  if (!status.ok()) throw std::runtime_error(status.message());
}
struct DeviceMemory {
  std::vector<void*> buffers_;
  template <typename T>
  T* Allocate(size_t count) {
    void* pointer = nullptr;
    Check(cudaMalloc(&pointer, count * sizeof(T)));
    buffers_.push_back(pointer);
    return static_cast<T*>(pointer);
  }
  ~DeviceMemory() {
    cudaDeviceSynchronize();
    for (void* pointer : buffers_) cudaFree(pointer);
  }
};
std::vector<uint16_t> Read(const uint16_t* pointer, size_t bytes) {
  std::vector<uint16_t> result(bytes / sizeof(uint16_t));
  Check(cudaMemcpy(result.data(), pointer, bytes, cudaMemcpyDeviceToHost));
  return result;
}

}  // namespace

Q4T_TEST(mtp_position_metadata_exact) {
  const std::string root =
      "/home/rm01/models/dev/llm/garnermccloud/"
      "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
  q4t::model::Model main;
  q4t::mtp::MtpModel draft;
  bool passed = false;
  try {
    q4t::model::ModelConfig config;
    config.model_dir = root;
    config.index_path = root + "/model.safetensors.index.json";
    config.ple_sidecar = root + "/ple/qwen3.8-flash-next-ple-fp8.bin";
    config.num_layers = 2;
    config.max_prefill = 8;
    Check(q4t::model::LoadModel(config, &main, nullptr));
    q4t::mtp::MtpConfig mtp_config;
    mtp_config.mtp_dir = root + "/mtp";
    mtp_config.max_seq = 4;
    mtp_config.max_len = 8224;
    mtp_config.max_prefill = 8;
    Check(q4t::mtp::LoadMtp(mtp_config, main.head.embed_tokens,
                            main.head.lm_head, &draft, nullptr));
    DeviceMemory memory;
    constexpr int kRows = 8;
    const size_t hs = draft.cfg.hs;
    const size_t hidden = hs * draft.cfg.hc;
    const size_t vocab = draft.cfg.vocab;
    auto* ids = memory.Allocate<int32_t>(kRows);
    auto* positions = memory.Allocate<int>(kRows);
    auto* slots = memory.Allocate<int>(kRows);
    auto* input = memory.Allocate<uint16_t>(kRows * hidden);
    auto* sample = memory.Allocate<uint16_t>(kRows * hs);
    auto* multi = memory.Allocate<uint16_t>(kRows * hidden);
    auto* logits = memory.Allocate<uint16_t>(kRows * vocab);
    std::vector<int32_t> host_ids = {846, 25, 1203, 321, 97, 131, 211, 313};
    std::vector<uint16_t> host_input(kRows * hidden);
    for (size_t i = 0; i < host_input.size(); ++i) {
      host_input[i] = static_cast<uint16_t>(0x3c00 + i % 127);
    }
    Check(cudaMemcpy(ids, host_ids.data(), kRows * sizeof(int32_t),
                     cudaMemcpyHostToDevice));
    Check(cudaMemcpy(input, host_input.data(), host_input.size() * 2,
                     cudaMemcpyHostToDevice));
    // Real row maxima cross 4-token compression and 2048/2049-group dispatch.
    // Four-sequence cases include a maximum in slot 1, with slot order 2,0,3,1.
    int cases = 0;
    for (int maximum : {3, 4, 8191, 8192, 8194, 8195, 8196}) {
      for (int rows : {1, 2, 3, 4, 5, 6, 7, 8}) {
        std::vector<int> host_pos(rows), host_slots(rows);
        const int slot_order[] = {2, 0, 3, 1};
        for (int row = 0; row < rows; ++row) {
          host_slots[row] = slot_order[row / (rows > 4 ? 2 : 1)];
          host_pos[row] = maximum - (rows - 1 - row) % 4;
        }
        Check(cudaMemcpy(positions, host_pos.data(), rows * sizeof(int),
                         cudaMemcpyHostToDevice));
        Check(cudaMemcpy(slots, host_slots.data(), rows * sizeof(int),
                         cudaMemcpyHostToDevice));
        std::vector<std::vector<uint16_t>> reference;
        for (int explicit_max : {-1, maximum}) {
          Check(q4t::mtp::MtpResetState(draft, nullptr));
          // Two forwards also check subsequent state-dependent computation.
          for (int step = 0; step < 2; ++step) {
            Check(q4t::mtp::MtpForward(
                draft, ids, positions, input, sample, multi, logits, rows,
                nullptr, slots, true, explicit_max));
            Check(cudaDeviceSynchronize());
            std::vector<std::vector<uint16_t>> observed = {
                Read(sample, rows * hs * 2),
                Read(multi, rows * hidden * 2),
                Read(logits, rows * vocab * 2),
                Read(draft.kv_cache, draft.kv_bytes * draft.max_seq),
                Read(draft.idx_raw, draft.idx_bytes * draft.max_seq),
                Read(draft.idx_comp, draft.idx_bytes * draft.max_seq)};
            for (size_t field = 0; field < 3; ++field) {
              for (uint16_t value : observed[field]) {
                const float number = std::bit_cast<float>(
                    static_cast<uint32_t>(value) << 16);
                if (!std::isfinite(number)) {
                  throw std::runtime_error("non-finite metadata output");
                }
              }
            }
            if (explicit_max == -1) {
              reference.insert(reference.end(), observed.begin(),
                               observed.end());
            } else {
              for (size_t field = 0; field < observed.size(); ++field) {
                if (observed[field] != reference[step * 6 + field]) {
                  throw std::runtime_error("metadata differs: max=" +
                      std::to_string(maximum) + " rows=" +
                      std::to_string(rows) + " field=" +
                      std::to_string(field));
                }
              }
            }
          }
        }
        ++cases;
        std::printf("metadata max=%d rows=%d steps=2 "
                    "logits_hidden_kv_raw_comp_bit_exact=1\n", maximum, rows);
      }
    }
    passed = cases == 56;
  } catch (const std::exception& error) {
    std::fprintf(stderr, "metadata failure: %s\n", error.what());
  }
  draft.Free();
  main.Free();
  return passed;
}
