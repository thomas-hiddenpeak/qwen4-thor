// Independent checkpoint layout regression: identical verify arithmetic must
// restore identical recurrent state regardless of reserved checkpoint capacity.
// A cap=1 run supplies the tightly packed reference for a cap=3, T=2 verify.
// A separate same-B/T comparison validates compact SSM record replay against
// the old complete snapshots. These are two-layer contracts, not quality tests.
#include "q4t/model/model_owner.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using q4t::model::Model;

void RequireCondition(bool condition, const char* operation) {
  if (!condition) throw std::runtime_error(operation);
}

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

std::vector<int32_t> CheckpointPrompt() {
  std::vector<int32_t> prompt;
  for (int i = 0; i < 13; ++i) prompt.push_back(42 + 17 * i);
  return prompt;
}

q4t::model::ModelConfig CheckpointModelConfig() {
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
  return cfg;
}

RunResult Run(Model& model, int capacity, uint16_t* logits) {
  Require(q4t::model::ModelReserveVerifyCheckpoints(model, capacity),
          "reserve");
  if (model.verify_ckpt_cap != capacity)
    throw std::runtime_error("unexpected checkpoint capacity");
  PoisonCheckpoints(model);
  const auto prompt = CheckpointPrompt();
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
  RequireCondition(!model.verify_ssm_record_fold,
                   "T=2 must retain the one-row snapshot fallback");
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

// Only the anchor plus records 1..row can affect this accepted boundary.
// Poison every later record and unused allocation byte with FP32/BF16 NaNs.
// The reference layout gets the same rejected-prefix poison independently.
void PoisonRejectedCheckpointTail(const Model& model, int row) {
  RequireCondition(model.cfg.max_seq == 1 && row >= 0 &&
                       row < model.verify_ckpt_rows,
                   "checkpoint poison requires a valid single-seq row");
  const size_t cap = static_cast<size_t>(model.verify_ckpt_cap);
  size_t ssm_offset = 0, conv_offset = 0, ple_offset = 0;
  for (const auto& layer : model.layers) {
    if (layer.is_full_attention) continue;
    const size_t nssm = static_cast<size_t>(layer.linear.nv) *
                        layer.linear.kd * layer.linear.vd;
    const size_t nrecord = static_cast<size_t>(layer.linear.nv) *
                           (layer.linear.kd + layer.linear.vd + 2);
    const size_t nconv = static_cast<size_t>(layer.linear.in_qkv()) *
                         (layer.linear.conv_k - 1);
    const size_t used = model.verify_ssm_record_fold
                            ? nssm + static_cast<size_t>(row) * nrecord
                            : static_cast<size_t>(row + 1) * nssm;
    RequireCondition(used <= cap * nssm, "record prefix exceeds allocation");
    RequireCuda(cudaMemset(model.d_verify_ssm_ckpt + ssm_offset + used,
                           0xff, (cap * nssm - used) * sizeof(float)),
                "poison rejected SSM records");
    const size_t kept_conv = static_cast<size_t>(row + 1) * nconv;
    RequireCuda(cudaMemset(model.d_verify_conv_ckpt + conv_offset + kept_conv,
                           0xff, (cap * nconv - kept_conv) * sizeof(uint16_t)),
                "poison rejected conv checkpoints");
    ssm_offset += cap * nssm;
    conv_offset += cap * nconv;
    if (layer.has_ple) {
      const size_t nple = static_cast<size_t>(layer.hc_dim) *
                          (layer.ple.conv_kernel - 1) *
                          layer.ple.conv_dilation;
      const size_t kept_ple = static_cast<size_t>(row + 1) * nple;
      RequireCuda(cudaMemset(model.d_verify_ple_conv_ckpt + ple_offset +
                               kept_ple,
                             0xff, (cap * nple - kept_ple) * sizeof(uint16_t)),
                  "poison rejected PLE checkpoints");
      ple_offset += cap * nple;
    }
  }
}

void RequireNonzeroSsm(const std::vector<Observation>& state) {
  size_t checked = 0;
  for (const auto& item : state) {
    if (!item.name.ends_with(".ssm")) continue;
    bool nonzero = false;
    for (size_t offset = 0; offset < item.bytes.size();
         offset += sizeof(float)) {
      float value;
      std::memcpy(&value, item.bytes.data() + offset, sizeof(value));
      RequireCondition(std::isfinite(value), "nonfinite initial SSM state");
      nonzero |= value != 0.0f;
    }
    RequireCondition(nonzero,
                     "record/fold fixture must start from nonzero SSM");
    ++checked;
  }
  RequireCondition(checked == 2, "record/fold fixture requires two SSM layers");
}

struct RecordFoldRound {
  std::vector<Observation> initial;
  std::vector<Observation> verified;
  std::vector<Observation> accepted;
  std::vector<Observation> continued;
  Observation verify_logits;
  Observation probe_logits;
};

// Both arms use identical B/T and inputs. The old full-SSM snapshot writer is
// the comparison arm; it never uses the compact replay reader. Existing
// independent-prefix tests separately validate that snapshot contract.
std::array<RecordFoldRound, 2> RunRecordFoldBoundaries(
    Model& model, bool compact, int first_accepted_drafts, uint16_t* logits) {
  model.enable_verify_ssm_record_fold = compact;
  Require(q4t::model::ModelReserveVerifyCheckpoints(model, 4),
          "reserve cap=4 for three saved rows");
  const auto prompt = CheckpointPrompt();
  q4t::model::ModelSequence sequence;
  Require(q4t::model::ModelBeginSequence(model, &sequence, nullptr, 0),
          "begin record/fold sequence");
  RequireCondition(!model.verify_ssm_record_fold,
                   "begin must invalidate prior record representation");
  Require(q4t::model::ModelPrefill(
              model, &sequence, prompt.data(), static_cast<int>(prompt.size()),
              logits, nullptr, nullptr, nullptr, 0,
              q4t::model::LogitsRows::kLastRow),
          "prefill nonzero record/fold state");
  const std::array<std::array<int32_t, 4>, 2> inputs = {
      {{97, 131, 211, 307}, {503, 607, 709, 811}}};
  std::array<RecordFoldRound, 2> result;
  for (size_t round = 0; round < result.size(); ++round) {
    auto& observed = result[round];
    const int accepted_drafts =
        (first_accepted_drafts + static_cast<int>(round)) % 4;
    const auto& tokens = inputs[round];
    const int slot = 0;
    observed.initial = CaptureState(model);
    RequireNonzeroSsm(observed.initial);
    PoisonCheckpoints(model);
    Require(q4t::model::ModelVerifyMulti(
                model, tokens.data(), &sequence.position, &slot,
                sequence.history.data(),
                static_cast<int>(sequence.history.size()), 1, 4, logits,
                nullptr),
            "verify B=1 T=4 record/fold boundary");
    RequireCondition(model.verify_ssm_record_fold == compact &&
                         model.verify_ckpt_rows == 3 &&
                         model.verify_ckpt_cap == 4,
                     "record/fold dispatch or valid-row mismatch");
    observed.verified = CaptureState(model);
    observed.verify_logits =
        Read("verify_logits", logits,
             static_cast<size_t>(4) * model.cfg.vocab * sizeof(uint16_t));
    if (accepted_drafts < 3) {
      q4t::model::ModelStateSnapshot checkpoint;
      Require(q4t::model::ModelSnapshotCheckpoint(
                  model, accepted_drafts, &checkpoint, nullptr, 0),
              "snapshot accepted checkpoint without changing live state");
      RequireCondition(checkpoint.valid &&
                           checkpoint.data.size() == checkpoint.bytes,
                       "invalid reconstructed checkpoint snapshot");
      RequireCondition(CompareState("snapshot_preserves_live",
                                    CaptureState(model), observed.verified),
                       "checkpoint reconstruction changed live state");
      PoisonRejectedCheckpointTail(model, accepted_drafts);
      q4t::model::ModelStateSnapshot poisoned;
      Require(q4t::model::ModelSnapshotCheckpoint(
                  model, accepted_drafts, &poisoned, nullptr, 0),
              "snapshot with poisoned rejected tail");
      RequireCondition(poisoned.valid && poisoned.data == checkpoint.data,
                       "rejected tail contaminated checkpoint snapshot");
      RequireCondition(CompareState("poison_snapshot_preserves_live",
                                    CaptureState(model), observed.verified),
                       "poisoned checkpoint snapshot changed live state");
      Require(q4t::model::ModelRestoreCheckpoint(
                  model, accepted_drafts, nullptr, 0),
              "restore accepted boundary from anchor");
      observed.accepted = CaptureState(model);
      q4t::model::ModelStateSnapshot restored;
      Require(q4t::model::ModelSnapshotState(model, &restored, nullptr),
              "snapshot restored recurrent state");
      RequireCondition(restored.valid && restored.data == checkpoint.data,
                       "restore differs from reconstructed checkpoint");
      Require(q4t::model::ModelRestoreCheckpoint(
                  model, accepted_drafts, nullptr, 0),
              "repeat restore must restart from anchor");
      RequireCondition(CompareState("repeated_restore", CaptureState(model),
                                    observed.accepted),
                       "repeated restore applied records twice");
    } else {
      // All four verify inputs are accepted: production retains the live
      // final state and performs no replay or checkpoint restoration.
      observed.accepted = CaptureState(model);
    }
    sequence.history.insert(sequence.history.end(), tokens.begin(),
                            tokens.begin() + accepted_drafts + 1);
    sequence.position += accepted_drafts + 1;
    const int32_t probe = 911 + static_cast<int32_t>(round);
    Require(q4t::model::ModelDecodeStepSeq(model, &sequence, probe, logits,
                                          nullptr, nullptr, 0),
            "decode fixed continuation from accepted boundary");
    observed.probe_logits =
        Read("probe_logits", logits,
             static_cast<size_t>(model.cfg.vocab) * sizeof(uint16_t));
    observed.continued = CaptureState(model);
    RequireCondition(!model.verify_ssm_record_fold &&
                         !q4t::model::ModelRestoreCheckpoint(model, 0, nullptr,
                                                            0).ok(),
                     "ordinary decode left stale replay records restorable");
    q4t::model::ModelStateSnapshot stale;
    RequireCondition(!q4t::model::ModelSnapshotCheckpoint(
                          model, 0, &stale, nullptr, 0).ok(),
                     "snapshot accepted stale replay records");
    RequireCondition(CompareState("stale_rejection_preserves_live",
                                  CaptureState(model), observed.continued),
                     "stale replay rejection modified live state");
    std::printf("  record_fold=%d round=%zu accepted_drafts=%d "
                "accepted_tokens=%d rejected_tail_poison=%d\n",
                compact, round, accepted_drafts, accepted_drafts + 1,
                accepted_drafts < 3);
  }
  Require(q4t::model::ModelEndSequence(&sequence), "end record/fold sequence");
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
  auto snapshot = [&model](int row, int slot = 0) {
    q4t::model::ModelStateSnapshot state;
    return q4t::model::ModelSnapshotCheckpoint(model, row, &state, nullptr,
                                               slot);
  };
  // Run() ended with an ordinary model forward, so its verify is stale.
  rejected(restore(0), "ordinary_forward");
  rejected(snapshot(0), "snapshot_ordinary_forward");
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
  valid &= model.verify_ssm_record_fold;
  Require(restore(2), "last written row");
  RequireCuda(cudaStreamSynchronize(nullptr), "last-row restore completion");
  rejected(restore(-1), "negative_row");
  rejected(restore(3), "capacity_row");
  rejected(restore(0, -1), "negative_slot");
  rejected(restore(0, model.cfg.max_seq), "outside_slot");
  rejected(snapshot(-1), "snapshot_negative_row");
  rejected(snapshot(3), "snapshot_capacity_row");
  rejected(snapshot(0, -1), "snapshot_negative_slot");
  rejected(snapshot(0, model.cfg.max_seq), "snapshot_outside_slot");

  fresh();
  Require(verify(2), "replace three rows with one");
  RequireCuda(cudaStreamSynchronize(nullptr), "one-row completion");
  valid &= model.verify_ckpt_rows == 1;
  valid &= !model.verify_ssm_record_fold;
  Require(restore(0), "written row after shrinking active stride");
  Require(restore(0), "repeated current restore");
  RequireCuda(cudaStreamSynchronize(nullptr), "repeated restore completion");
  rejected(restore(1), "unwritten_row");
  rejected(restore(2), "old_verify_row");
  rejected(snapshot(1), "snapshot_unwritten_row");
  rejected(snapshot(2), "snapshot_old_verify_row");
  const auto before_reject = CaptureState(model);
  rejected(verify(5), "verify_exceeds_reserved_capacity");
  rejected(restore(0), "failed_verify");
  rejected(snapshot(0), "snapshot_failed_verify");
  valid &= !model.verify_ssm_record_fold;
  RequireCuda(cudaStreamSynchronize(nullptr), "rejected-work completion");
  valid &= CompareState("rejected_inputs_unchanged", CaptureState(model),
                        before_reject);
  // T>1 uses the single-sequence PLE checkpoint path in production.
  Require(q4t::model::ModelDecodeBatch(
              model, tokens, 2, sequence.position, sequence.history.data(),
              static_cast<int>(sequence.history.size()), nullptr, nullptr,
              nullptr, true, slot),
          "single-sequence PLE checkpoint");
  Require(restore(0), "single-sequence saved row");
  rejected(restore(1), "single-sequence unwritten row");

  fresh();
  Require(verify(1), "verify without saved rows");
  RequireCuda(cudaStreamSynchronize(nullptr), "zero-row completion");
  rejected(restore(0), "zero_saved_rows");
  valid &= model.verify_ckpt_rows == 0 && model.verify_ckpt_slots.empty();
  valid &= !model.verify_ssm_record_fold;
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
  const auto cfg = CheckpointModelConfig();
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

Q4T_TEST(mtp_ssm_record_fold_boundaries) {
  RequireCuda(cudaSetDevice(0), "CUDA required; no SKIP");
  for (const char* name : {"Q4T_GDN_REG", "Q4T_GDN_CHUNKED", "Q4T_GDN_SPLIT",
                         "Q4T_FP8_PROJ", "Q4T_FP8_HC", "Q4T_FP8_ALL"}) {
    Q4T_CHECK(std::getenv(name) == nullptr);
  }
  const auto cfg = CheckpointModelConfig();
  q4t::model::ModelOwner owner;
  Require(owner.Load(cfg, nullptr), "load record/fold model with PLE");
  Model& model = owner.Get();
  Q4T_CHECK(model.layers.size() == 2 && model.layers[1].has_ple);
  DeviceLogits logits(static_cast<size_t>(4) * cfg.vocab);
  bool equal = true;
  for (int accepted_drafts = 0; accepted_drafts < 4; ++accepted_drafts) {
    const auto reference =
        RunRecordFoldBoundaries(model, false, accepted_drafts, logits.data());
    const auto candidate =
        RunRecordFoldBoundaries(model, true, accepted_drafts, logits.data());
    for (size_t round = 0; round < reference.size(); ++round) {
      const std::string label = "accept" + std::to_string(accepted_drafts) +
                                ".round" + std::to_string(round);
      const auto& a = candidate[round];
      const auto& b = reference[round];
      equal &= CompareState(label + ".initial", a.initial, b.initial);
      equal &= CompareState(label + ".verified", a.verified, b.verified);
      equal &= CompareState(label + ".accepted", a.accepted, b.accepted);
      equal &= CompareState(label + ".continued", a.continued, b.continued);
      equal &= Compare(label, a.verify_logits, b.verify_logits);
      equal &= Compare(label, a.probe_logits, b.probe_logits);
    }
  }
  return equal;
}
