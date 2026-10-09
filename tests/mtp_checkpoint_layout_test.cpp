// Independent checkpoint layout regression: identical verify arithmetic must
// restore identical recurrent state regardless of reserved checkpoint capacity.
// A cap=1 run supplies the tightly packed reference for a cap=3, T=2 verify.
// This is a two-layer state-layout contract, not a model-quality oracle.
#include "q4t/model/model_owner.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using q4t::model::Model;

void Require(const q4t::Status& status, const char* operation) {
  if (!status.ok())
    throw std::runtime_error(std::string(operation) + ": " + status.message());
}

void RequireCuda(cudaError_t error, const char* operation) {
  if (error != cudaSuccess)
    throw std::runtime_error(std::string(operation) + ": " +
                             cudaGetErrorString(error));
}

class DeviceLogits {
 public:
  explicit DeviceLogits(size_t elements) {
    RequireCuda(cudaMalloc(reinterpret_cast<void**>(&data_),
                           elements * sizeof(uint16_t)),
                "allocate test logits");
  }
  ~DeviceLogits() {
    const cudaError_t error = cudaFree(data_);
    if (error != cudaSuccess) {
      std::fprintf(stderr, "free test logits: %s\n", cudaGetErrorString(error));
      std::abort();
    }
  }
  DeviceLogits(const DeviceLogits&) = delete;
  DeviceLogits& operator=(const DeviceLogits&) = delete;
  uint16_t* data() const { return data_; }

 private:
  uint16_t* data_ = nullptr;
};

struct Observation {
  std::string name;
  std::vector<uint8_t> bytes;
};

Observation Read(const std::string& name, const void* device, size_t bytes) {
  Observation result{name, std::vector<uint8_t>(bytes)};
  RequireCuda(
      cudaMemcpy(result.bytes.data(), device, bytes, cudaMemcpyDeviceToHost),
      "read checkpoint observation");
  return result;
}

std::vector<Observation> CaptureState(const Model& model) {
  std::vector<Observation> result;
  for (size_t i = 0; i < model.layers.size(); ++i) {
    const auto& layer = model.layers[i];
    const std::string name = "layer" + std::to_string(i);
    if (layer.ssm_state) {
      const size_t ssm_bytes = static_cast<size_t>(layer.linear.nv) *
                               layer.linear.kd * layer.linear.vd *
                               sizeof(float);
      const size_t conv_bytes = static_cast<size_t>(layer.linear.in_qkv()) *
                                (layer.linear.conv_k - 1) * sizeof(uint16_t);
      result.push_back(Read(name + ".ssm", layer.ssm_state, ssm_bytes));
      result.push_back(Read(name + ".conv", layer.conv_state, conv_bytes));
    }
    if (layer.ple_conv_state) {
      const size_t ple_bytes = static_cast<size_t>(layer.hc_dim) *
                               (layer.ple.conv_kernel - 1) *
                               layer.ple.conv_dilation * sizeof(uint16_t);
      result.push_back(Read(name + ".ple", layer.ple_conv_state, ple_bytes));
    }
  }
  return result;
}

bool Compare(const std::string& label, const Observation& actual,
             const Observation& expected) {
  if (actual.name != expected.name ||
      actual.bytes.size() != expected.bytes.size())
    throw std::runtime_error("observation shape mismatch");
  size_t unequal = 0;
  size_t first = actual.bytes.size();
  for (size_t i = 0; i < actual.bytes.size(); ++i) {
    if (actual.bytes[i] == expected.bytes[i]) continue;
    ++unequal;
    if (first == actual.bytes.size()) first = i;
  }
  std::printf("  compare=%s.%s bytes=%zu unequal=%zu first_byte=%zu\n",
              label.c_str(), actual.name.c_str(), actual.bytes.size(), unequal,
              first);
  return unequal == 0;
}

bool CompareState(const std::string& label,
                  const std::vector<Observation>& actual,
                  const std::vector<Observation>& expected) {
  if (actual.size() != expected.size())
    throw std::runtime_error("state layer count mismatch");
  bool equal = true;
  for (size_t i = 0; i < actual.size(); ++i)
    equal &= Compare(label, actual[i], expected[i]);
  return equal;
}

// Poison unused capacity so the old reader cannot accidentally find matching
// bytes in an allocation that happened to be reused. All sizes come from the
// loaded layers; this helper never relies on the disputed reader stride.
void PoisonCheckpoints(const Model& model) {
  size_t ssm_bytes = 0, conv_bytes = 0, ple_bytes = 0;
  for (const auto& layer : model.layers) {
    if (layer.is_full_attention) continue;
    ssm_bytes += static_cast<size_t>(layer.linear.nv) * layer.linear.kd *
                 layer.linear.vd * sizeof(float);
    conv_bytes += static_cast<size_t>(layer.linear.in_qkv()) *
                  (layer.linear.conv_k - 1) * sizeof(uint16_t);
    if (layer.has_ple)
      ple_bytes += static_cast<size_t>(layer.hc_dim) *
                   (layer.ple.conv_kernel - 1) * layer.ple.conv_dilation *
                   sizeof(uint16_t);
  }
  const size_t rows =
      static_cast<size_t>(model.cfg.max_seq) * model.verify_ckpt_cap;
  RequireCuda(cudaMemset(model.d_verify_ssm_ckpt, 0xa5, rows * ssm_bytes),
              "poison SSM checkpoints");
  RequireCuda(cudaMemset(model.d_verify_conv_ckpt, 0xa5, rows * conv_bytes),
              "poison conv checkpoints");
  if (ple_bytes != 0)
    RequireCuda(
        cudaMemset(model.d_verify_ple_conv_ckpt, 0xa5, rows * ple_bytes),
        "poison PLE checkpoints");
}

struct RunResult {
  std::vector<Observation> prefill;
  std::vector<Observation> verified;
  std::vector<Observation> restored;
  Observation verify_logits;
  Observation probe_logits;
};

RunResult Run(Model& model, int capacity, uint16_t* logits) {
  Require(q4t::model::ModelReserveVerifyCheckpoints(model, capacity),
          "reserve");
  if (model.verify_ckpt_cap != capacity)
    throw std::runtime_error("unexpected checkpoint capacity");
  PoisonCheckpoints(model);
  std::vector<int32_t> prompt;
  for (int i = 0; i < 13; ++i) prompt.push_back(42 + 17 * i);
  q4t::model::ModelSequence sequence;
  Require(q4t::model::ModelBeginSequence(model, &sequence, nullptr, 0),
          "begin");
  Require(q4t::model::ModelPrefill(model, &sequence, prompt.data(),
                                   static_cast<int>(prompt.size()), logits,
                                   nullptr, nullptr, nullptr, 0,
                                   q4t::model::LogitsRows::kLastRow),
          "prefill identical prompt");
  RunResult result;
  result.prefill = CaptureState(model);
  const int32_t tokens[2] = {97, 131};
  const int slot = 0;
  Require(q4t::model::ModelVerifyMulti(
              model, tokens, &sequence.position, &slot, sequence.history.data(),
              static_cast<int>(sequence.history.size()), 1, 2, logits, nullptr),
          "verify identical B=1 T=2 tokens");
  RequireCuda(cudaStreamSynchronize(nullptr), "verify completion");
  result.verified = CaptureState(model);
  result.verify_logits =
      Read("logits", logits,
           static_cast<size_t>(2) * model.cfg.vocab * sizeof(uint16_t));
  Require(q4t::model::ModelRestoreCheckpoint(model, 0, nullptr, 0),
          "restore 0");
  RequireCuda(cudaStreamSynchronize(nullptr), "restore completion");
  result.restored = CaptureState(model);

  // Feed an identical fixed continuation after rolling back to one accepted
  // token. ModelDecodeBatch writes its own RoPE, independently of Multi's
  // decode-position contract. The layout comparison above is already direct.
  sequence.history.push_back(tokens[0]);
  ++sequence.position;
  const int32_t probe = 503;
  Require(q4t::model::ModelDecodeBatch(
              model, &probe, 1, sequence.position, sequence.history.data(),
              static_cast<int>(sequence.history.size()), logits, nullptr),
          "fixed probe after restore");
  RequireCuda(cudaStreamSynchronize(nullptr), "probe completion");
  result.probe_logits =
      Read("logits", logits, model.cfg.vocab * sizeof(uint16_t));
  return result;
}

bool CheckValidity(Model& model) {
  bool valid = true;
  auto rejected = [&valid](const q4t::Status& status, const char* label) {
    std::printf("  checkpoint_reject=%s rejected=%d message=%s\n", label,
                !status.ok(), status.message().c_str());
    valid &= !status.ok();
  };
  auto restore = [&model](int row, int slot = 0) {
    return q4t::model::ModelRestoreCheckpoint(model, row, nullptr, slot);
  };
  // Run() ended with an ordinary model forward, so its verify is stale.
  rejected(restore(0), "ordinary_forward");
  rejected(q4t::model::ModelReserveVerifyCheckpoints(model, -1),
           "negative_capacity");
  const int32_t prompt[2] = {42, 59};
  const int32_t tokens[5] = {97, 131, 211, 307, 503};
  const int slot = 0;
  q4t::model::ModelSequence sequence;
  auto fresh = [&] {
    Require(q4t::model::ModelBeginSequence(model, &sequence, nullptr, slot),
            "validity begin");
    rejected(restore(0), "sequence_reset");
    Require(
        q4t::model::ModelPrefill(model, &sequence, prompt, 2, nullptr, nullptr),
        "validity prefill");
  };
  auto verify = [&](int count) {
    return q4t::model::ModelVerifyMulti(
        model, tokens, &sequence.position, &slot, sequence.history.data(),
        static_cast<int>(sequence.history.size()), 1, count, nullptr, nullptr);
  };
  fresh();
  Require(verify(4), "verify three saved rows");
  RequireCuda(cudaStreamSynchronize(nullptr), "three-row completion");
  valid &= model.verify_ckpt_rows == 3;
  Require(restore(2), "last written row");
  RequireCuda(cudaStreamSynchronize(nullptr), "last-row restore completion");
  rejected(restore(-1), "negative_row");
  rejected(restore(3), "capacity_row");
  rejected(restore(0, -1), "negative_slot");
  rejected(restore(0, model.cfg.max_seq), "outside_slot");

  fresh();
  Require(verify(2), "replace three rows with one");
  RequireCuda(cudaStreamSynchronize(nullptr), "one-row completion");
  valid &= model.verify_ckpt_rows == 1;
  Require(restore(0), "written row after shrinking active stride");
  Require(restore(0), "repeated current restore");
  RequireCuda(cudaStreamSynchronize(nullptr), "repeated restore completion");
  rejected(restore(1), "unwritten_row");
  rejected(restore(2), "old_verify_row");
  const auto before_reject = CaptureState(model);
  rejected(verify(5), "verify_exceeds_reserved_capacity");
  rejected(restore(0), "failed_verify");
  const q4t::Status legacy = q4t::model::ModelDecodeBatch(
      model, tokens, 2, sequence.position, sequence.history.data(),
      static_cast<int>(sequence.history.size()), nullptr, nullptr, nullptr,
      true, slot);
  rejected(legacy, "legacy_single_sequence_ple_checkpoint");
  valid &= legacy.message().find("ModelVerifyMulti") != std::string::npos;
  RequireCuda(cudaStreamSynchronize(nullptr), "rejected-work completion");
  valid &= CompareState("rejected_inputs_unchanged", CaptureState(model),
                        before_reject);

  fresh();
  Require(verify(1), "verify without saved rows");
  RequireCuda(cudaStreamSynchronize(nullptr), "zero-row completion");
  rejected(restore(0), "zero_saved_rows");
  valid &= model.verify_ckpt_rows == 0 && model.verify_ckpt_slots.empty();
  std::printf("  checkpoint_validity=%d\n", valid);
  return valid;
}

}  // namespace

Q4T_TEST(mtp_checkpoint_capacity_layout) {
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
  cfg.num_layers = 2;
  cfg.max_len = 64;
  cfg.max_prefill = 32;
  cfg.max_seq = 1;
  cfg.ple_capacity_tokens = 32;
  q4t::model::ModelOwner owner;
  Require(owner.Load(cfg, nullptr), "load two-layer main model");
  Model& model = owner.Get();
  Q4T_CHECK(model.layers.size() == 2 && !model.layers[0].is_full_attention &&
            !model.layers[1].is_full_attention && model.layers[1].has_ple);
  DeviceLogits logits(static_cast<size_t>(2) * cfg.vocab);
  const RunResult cap1 = Run(model, 1, logits.data());
  const RunResult cap3 = Run(model, 3, logits.data());
  bool input_equal = CompareState("prefill", cap3.prefill, cap1.prefill);
  input_equal &= CompareState("verified", cap3.verified, cap1.verified);
  input_equal &= Compare("verify", cap3.verify_logits, cap1.verify_logits);
  const bool restore_equal =
      CompareState("restored", cap3.restored, cap1.restored);
  const bool probe_equal =
      Compare("probe", cap3.probe_logits, cap1.probe_logits);
  std::printf(
      "  checkpoint_capacity input_equal=%d restore_equal=%d "
      "probe_equal=%d cap1=1 cap3=3 saved_rows=1\n",
      input_equal, restore_equal, probe_equal);
  const bool validity = CheckValidity(model);
  return input_equal && restore_equal && probe_equal && validity;
}
