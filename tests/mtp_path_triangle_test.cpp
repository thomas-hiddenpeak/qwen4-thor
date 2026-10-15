// Bounded main-path diagnostic: plain B1 T1 vs verify B1 T1 vs verify B1 T4.
// No MTP draft weights are loaded. Fresh prefill establishes the same initial
// state before each call. Equal-shape T1 entry points must agree bitwise;
// cross-shape and suffix changes are observations, not numerical tolerances.
// Four real layers cover linear attention, PLE and full attention. This test
// does not establish full-model quality or MTP admission.
#include "q4t/model/model_owner.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <numeric>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {

using q4t::model::Model;
using q4t::model::ModelSequence;

void Require(const q4t::Status& status, const char* operation) {
  if (!status.ok())
    throw std::runtime_error(std::string(operation) + ": " + status.message());
}

void RequireCuda(cudaError_t error, const char* operation) {
  if (error != cudaSuccess)
    throw std::runtime_error(std::string(operation) + ": " +
                             cudaGetErrorString(error));
}

class DeviceBuffer {
 public:
  explicit DeviceBuffer(size_t elements) {
    RequireCuda(cudaMalloc(reinterpret_cast<void**>(&data_),
                           elements * sizeof(uint16_t)),
                "allocate observation buffer");
  }
  ~DeviceBuffer() {
    const cudaError_t error = cudaFree(data_);
    if (error != cudaSuccess) {
      std::fprintf(stderr, "free observation buffer: %s\n",
                   cudaGetErrorString(error));
      std::abort();
    }
  }
  DeviceBuffer(const DeviceBuffer&) = delete;
  DeviceBuffer& operator=(const DeviceBuffer&) = delete;
  uint16_t* data() const { return data_; }

 private:
  uint16_t* data_ = nullptr;
};

float Value(uint16_t value) {
  const uint32_t bits = static_cast<uint32_t>(value) << 16;
  float result;
  std::memcpy(&result, &bits, sizeof(result));
  return result;
}
float Value(float value) { return value; }
double Value(int value) { return value; }

template <typename T>
std::vector<T> Read(const std::string& label, const T* device, size_t count) {
  std::vector<T> result(count);
  RequireCuda(cudaMemcpy(result.data(), device, count * sizeof(T),
                         cudaMemcpyDeviceToHost),
              "read observation");
  size_t nonfinite = 0;
  for (T value : result) nonfinite += !std::isfinite(Value(value));
  std::printf("  finite=%s elements=%zu nonfinite=%zu\n", label.c_str(), count,
              nonfinite);
  if (nonfinite != 0)
    throw std::runtime_error("non-finite observation: " + label);
  return result;
}

template <typename T>
bool Compare(const std::string& label, const std::vector<T>& actual,
             const std::vector<T>& expected) {
  if (actual.size() != expected.size())
    throw std::runtime_error("observation shape mismatch: " + label);
  size_t unequal = 0, first = actual.size();
  double squared = 0, reference_squared = 0, max_abs = 0;
  for (size_t i = 0; i < actual.size(); ++i) {
    if (std::memcmp(&actual[i], &expected[i], sizeof(T)) != 0) {
      ++unequal;
      if (first == actual.size()) first = i;
    }
    const double a = Value(actual[i]), b = Value(expected[i]);
    squared += (a - b) * (a - b);
    reference_squared += b * b;
    max_abs = std::max(max_abs, std::abs(a - b));
  }
  const double relative_l2 = reference_squared > 0
                                 ? std::sqrt(squared / reference_squared)
                                 : std::sqrt(squared);
  std::printf(
      "  compare=%s elements=%zu unequal=%zu first=%zu "
      "max_abs=%.9g relative_l2=%.9g\n",
      label.c_str(), actual.size(), unequal, first, max_abs, relative_l2);
  return unequal == 0;
}

struct StateTensor {
  std::string name;
  std::vector<float> fp32;
  std::vector<uint16_t> bf16;
  std::vector<int> ints;
};
using State = std::vector<StateTensor>;

State CaptureState(const Model& model, const std::string& label,
                   bool include_attention) {
  State result;
  const auto add_fp32 = [&](const std::string& name, const float* data,
                            size_t count) {
    result.push_back({name, Read(label + "." + name, data, count), {}, {}});
  };
  const auto add_bf16 = [&](const std::string& name, const uint16_t* data,
                            size_t count) {
    result.push_back({name, {}, Read(label + "." + name, data, count), {}});
  };
  const auto add_ints = [&](const std::string& name, const int* data,
                            size_t count) {
    result.push_back({name, {}, {}, Read(label + "." + name, data, count)});
  };
  for (size_t i = 0; i < model.layers.size(); ++i) {
    const auto& layer = model.layers[i];
    const std::string name = "layer" + std::to_string(i);
    if (layer.ssm_state) {
      add_fp32(name + ".ssm", layer.ssm_state,
               static_cast<size_t>(layer.linear.nv) * layer.linear.kd *
                   layer.linear.vd);
      add_bf16(name + ".conv", layer.conv_state,
               static_cast<size_t>(layer.linear.in_qkv()) *
                   (layer.linear.conv_k - 1));
    }
    if (layer.ple_conv_state) {
      add_bf16(name + ".ple", layer.ple_conv_state,
               static_cast<size_t>(layer.hc_dim) * (layer.ple.conv_kernel - 1) *
                   layer.ple.conv_dilation);
    }
    if (include_attention && layer.kv_cache) {
      const size_t pages = (layer.max_len + q4t::model::kKvPageSize - 1) /
                           q4t::model::kKvPageSize;
      add_bf16(
          name + ".kv", layer.kv_cache,
          pages * q4t::model::kKvPageSize * layer.full.nkv * 2 * layer.full.hd);
      add_bf16(name + ".index_raw", layer.idx_raw,
               static_cast<size_t>(layer.max_len) * layer.full.idx_head_dim);
      add_bf16(name + ".index_comp", layer.idx_comp,
               static_cast<size_t>(layer.max_len) * layer.full.idx_head_dim);
      add_ints(name + ".page_table", layer.page_table, layer.max_len);
    }
  }
  if (include_attention) {
    add_ints("rope", model.d_rope_pos,
             static_cast<size_t>(3) * model.cfg.max_len);
    result.push_back({"rope_delta", {}, {}, model.rope_delta});
  }
  return result;
}

bool CompareState(const std::string& label, const State& actual,
                  const State& expected) {
  if (actual.size() != expected.size())
    throw std::runtime_error("state component count mismatch: " + label);
  bool equal = true;
  for (size_t i = 0; i < actual.size(); ++i) {
    const auto& a = actual[i];
    const auto& b = expected[i];
    if (a.name != b.name)
      throw std::runtime_error("state component mismatch: " + label);
    if (!a.fp32.empty() || !b.fp32.empty())
      equal &= Compare(label + "." + a.name, a.fp32, b.fp32);
    if (!a.bf16.empty() || !b.bf16.empty())
      equal &= Compare(label + "." + a.name, a.bf16, b.bf16);
    if (!a.ints.empty() || !b.ints.empty())
      equal &= Compare(label + "." + a.name, a.ints, b.ints);
  }
  return equal;
}

// All vocab entries participate; equal BF16 values prefer the lowest ID.
int PrintTopFive(const std::string& label,
                 const std::vector<uint16_t>& logits) {
  if (logits.size() < 5) throw std::runtime_error("vocab too small");
  std::vector<size_t> order(logits.size());
  std::iota(order.begin(), order.end(), size_t{0});
  std::partial_sort(order.begin(), order.begin() + 5, order.end(),
                    [&](size_t a, size_t b) {
                      const float av = Value(logits[a]);
                      const float bv = Value(logits[b]);
                      return av != bv ? av > bv : a < b;
                    });
  const double best = Value(logits[order[0]]);
  std::printf("  top5=%s vocab=%zu top1_top2_margin=%.9g", label.c_str(),
              logits.size(), best - Value(logits[order[1]]));
  for (int rank = 0; rank < 5; ++rank) {
    const size_t id = order[rank];
    std::printf(" rank%d_id=%zu value=%.9g bits=%04x gap=%.9g", rank + 1, id,
                static_cast<double>(Value(logits[id])),
                static_cast<unsigned int>(logits[id]),
                best - Value(logits[id]));
  }
  std::printf("\n");
  return static_cast<int>(order[0]);
}

struct Observation {
  std::vector<uint16_t> logits;
  std::vector<uint16_t> trunk;
  State state;
};

void MarkForward(const std::string& label, int rows, int linear_layers,
                 int* forward_index) {
  const int begin = *forward_index * linear_layers;
  std::printf(
      "TRIANGLE_FORWARD index=%d stage=%s T=%d "
      "linear_hook_first=%d linear_hook_last=%d\n",
      *forward_index, label.c_str(), rows, begin, begin + linear_layers - 1);
  std::fflush(stdout);
  ++*forward_index;
}

bool CompareObservation(const std::string& label, const Observation& actual,
                        const Observation& expected) {
  bool equal = Compare(label + ".logits", actual.logits, expected.logits);
  equal &= Compare(label + ".trunk", actual.trunk, expected.trunk);
  equal &= CompareState(label + ".state", actual.state, expected.state);
  return equal;
}

}  // namespace

Q4T_TEST(mtp_path_triangle_single_stream) {
  RequireCuda(cudaSetDevice(0), "CUDA required; no SKIP");
  for (const char* name : {"Q4T_GDN_REG", "Q4T_GDN_CHUNKED", "Q4T_GDN_SPLIT",
                           "Q4T_FP8_PROJ", "Q4T_FP8_HC", "Q4T_FP8_ALL"}) {
    Q4T_CHECK(std::getenv(name) == nullptr);
  }
  q4t::model::ModelConfig cfg;
  cfg.model_dir =
      "/home/rm01/models/dev/llm/garnermccloud/"
      "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
  cfg.index_path = cfg.model_dir + "/model.safetensors.index.json";
  cfg.ple_sidecar = cfg.model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
  cfg.num_layers = 4;
  cfg.max_len = 64;
  cfg.max_prefill = 32;
  cfg.max_seq = 1;
  cfg.ple_capacity_tokens = 32;
  q4t::model::ModelOwner owner;
  Require(owner.Load(cfg, nullptr), "load four-layer main model");
  Model& model = owner.Get();
  Require(q4t::model::ModelReserveVerifyCheckpoints(model, 3), "checkpoints");

  std::vector<int32_t> prompt;
  for (int i = 0; i < 13; ++i) prompt.push_back(42 + 17 * i);
  DeviceBuffer logits(static_cast<size_t>(4) * cfg.vocab);
  DeviceBuffer trunk(prompt.size() * model.hc_dim());
  int linear_layers = 0;
  for (const auto& layer : model.layers)
    linear_layers += !layer.is_full_attention;
  int forward_index = 0;
  bool prefill_equal = true, t1_equal = true, bonus_equal = true;
  Observation reference_prefill;
  const char* paths[] = {"plain_t1", "verify_t1", "verify_t4_a", "verify_t4_b"};

  std::printf(
      "TRIANGLE_SCOPE layers=4 B=1 default_gdn=1 draft_loaded=0 "
      "prompt=42+17*i prompt_length=13 capacity=64 prefill_cap=32 "
      "cross_shape=diagnostic suffix_change=diagnostic "
      "hook_ordinals_require_fresh_process=1\n");
  for (int example = 0; example < 2; ++example) {
    std::vector<Observation> observations;
    for (int path = 0; path < 4; ++path) {
      const std::string label =
          std::string(example == 0 ? "fixed97." : "bonus287.") + paths[path];
      ModelSequence seq;
      Require(q4t::model::ModelBeginSequence(model, &seq, nullptr, 0), "begin");
      MarkForward(label + ".prefill", static_cast<int>(prompt.size()),
                  linear_layers, &forward_index);
      Require(q4t::model::ModelPrefill(
                  model, &seq, prompt.data(), static_cast<int>(prompt.size()),
                  logits.data(), nullptr, trunk.data(), nullptr, 0,
                  q4t::model::LogitsRows::kLastRow),
              "prefill");
      RequireCuda(cudaStreamSynchronize(nullptr), "prefill completion");
      Q4T_CHECK(seq.position == static_cast<int>(prompt.size()) &&
                seq.history == prompt && seq.seq_id == 0 &&
                seq.stage == ModelSequence::Stage::kDecode &&
                !seq.HasPending());
      Observation prefill{
          Read(label + ".prefill.logits", logits.data(), cfg.vocab),
          Read(label + ".prefill.trunk", trunk.data(),
               prompt.size() * model.hc_dim()),
          CaptureState(model, label + ".prefill.state", true)};
      const int bonus = PrintTopFive(label + ".prefill", prefill.logits);
      bonus_equal &= bonus == 287;
      if (example == 0 && path == 0)
        reference_prefill = prefill;
      else
        prefill_equal &= CompareObservation(label + ".prefill_replay", prefill,
                                            reference_prefill);

      const int32_t token = example == 0 ? 97 : bonus;
      std::vector<int32_t> inputs{token};
      if (path == 2) inputs.insert(inputs.end(), {131, 211, 307});
      if (path == 3) inputs.insert(inputs.end(), {503, 701, 907});
      std::printf("TRIANGLE_INPUT stage=%s position=%d tokens=", label.c_str(),
                  seq.position);
      for (size_t i = 0; i < inputs.size(); ++i)
        std::printf("%s%d", i == 0 ? "" : ",", inputs[i]);
      std::printf("\n");
      MarkForward(label + ".forward", static_cast<int>(inputs.size()),
                  linear_layers, &forward_index);
      const int slot = 0;
      if (path == 0) {
        const int width = model.ple_hash.ngram_size - 1;
        std::vector<int32_t> history(width, cfg.eos_token_id);
        for (int i = 0; i < width; ++i) {
          const int source = seq.position - width + i;
          if (source >= 0) history[i] = seq.history.at(source);
        }
        Require(q4t::model::ModelDecodeBatchMulti(
                    model, inputs.data(), &seq.position, &slot, history.data(),
                    1, logits.data(), nullptr, trunk.data()),
                "plain B1 T1");
      } else {
        Require(q4t::model::ModelVerifyMulti(
                    model, inputs.data(), &seq.position, &slot,
                    seq.history.data(), static_cast<int>(seq.history.size()), 1,
                    static_cast<int>(inputs.size()), logits.data(), nullptr,
                    trunk.data()),
                "verify B1");
      }
      RequireCuda(cudaStreamSynchronize(nullptr), "forward completion");
      // Scan every output row, not just the first row used for comparisons.
      auto all_logits =
          Read(label + ".all_logits", logits.data(), inputs.size() * cfg.vocab);
      auto all_trunk = Read(label + ".all_trunk", trunk.data(),
                            inputs.size() * model.hc_dim());
      Observation observation{
          std::vector<uint16_t>(all_logits.begin(),
                                all_logits.begin() + cfg.vocab),
          std::vector<uint16_t>(all_trunk.begin(),
                                all_trunk.begin() + model.hc_dim()),
          {}};
      PrintTopFive(label + ".first_row", observation.logits);
      if (inputs.size() > 1) {
        std::printf("TRIANGLE_RESTORE stage=%s checkpoint=0 rows=%d\n",
                    label.c_str(), model.verify_ckpt_rows);
        Require(q4t::model::ModelRestoreCheckpoint(model, 0, nullptr, 0),
                "restore prefix checkpoint zero");
        RequireCuda(cudaStreamSynchronize(nullptr), "restore completion");
      }
      observation.state = CaptureState(model, label + ".prefix_state", false);
      observations.push_back(std::move(observation));
    }
    const std::string label = example == 0 ? "fixed97" : "bonus287";
    t1_equal &= CompareObservation(label + ".verify_t1_vs_plain_t1",
                                   observations[1], observations[0]);
    const bool shape_a = CompareObservation(label + ".verify_t4_a_vs_verify_t1",
                                            observations[2], observations[1]);
    const bool shape_b = CompareObservation(label + ".verify_t4_b_vs_verify_t1",
                                            observations[3], observations[1]);
    const bool suffix =
        CompareObservation(label + ".verify_t4_b_vs_verify_t4_a",
                           observations[3], observations[2]);
    std::printf(
        "TRIANGLE_DIAGNOSTIC example=%s shape_a_equal=%d "
        "shape_b_equal=%d suffix_equal=%d acceptance_threshold=none\n",
        label.c_str(), shape_a, shape_b, suffix);
  }
  std::printf(
      "MTP_PATH_TRIANGLE prefill_equal=%d t1_equal=%d bonus287=%d "
      "all_observations_finite=1 forwards=%d "
      "cross_shape_and_suffix_are_diagnostic=1\n",
      prefill_equal, t1_equal, bonus_equal, forward_index);
  return prefill_equal && t1_equal && bonus_equal;
}
