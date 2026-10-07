// Fixed full-model k=3 selection and same-shape state replay diagnostic.
// Exactly 16 natural steps and one predetermined forced-d0 step; no retries.
// Recurrent snapshots omit KV/indexer: replay overwrites the same absolute
// positions, and causal masks exclude later positions. S=1, slot=0, text RoPE
// and the unchanged host prefix are required. This is not performance evidence
// or a plain/MTP equality test. Restore row oracles read checkpoint bytes
// directly, independently of the production checkpoint reader.
#include "q4t/model/model_owner.h"
#include "q4t/mtp/mtp.h"
#include "q4t/test.h"
#include "q4t/text/tokenizer.h"

#include <cuda_runtime.h>

#include <algorithm>
#include <array>
#include <bit>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using q4t::model::Model;
using q4t::model::ModelSequence;
using q4t::model::ModelStateSnapshot;
constexpr int kDrafts = 3, kRows = 4, kNaturalSteps = 16;
constexpr size_t kArtifactLimit = 64u * 1024u * 1024u;

void Require(bool condition, const std::string& operation) {
  if (!condition) throw std::runtime_error(operation);
}
void RequireStatus(const q4t::Status& status, const char* operation) {
  Require(status.ok(), std::string(operation) + ": " + status.message());
}
void RequireCuda(cudaError_t error, const char* operation) {
  Require(error == cudaSuccess,
          std::string(operation) + ": " + cudaGetErrorString(error));
}

class DeviceBuffer {
 public:
  explicit DeviceBuffer(size_t elements) {
    RequireCuda(cudaMalloc(reinterpret_cast<void**>(&data_),
                           elements * sizeof(uint16_t)),
                "allocate test buffer");
  }
  ~DeviceBuffer() {
    if (cudaFree(data_) != cudaSuccess) std::abort();
  }
  DeviceBuffer(const DeviceBuffer&) = delete;
  DeviceBuffer& operator=(const DeviceBuffer&) = delete;
  uint16_t* data() const { return data_; }

 private:
  uint16_t* data_ = nullptr;
};
struct MtpOwner {
  q4t::mtp::MtpModel model;
  ~MtpOwner() { model.Free(); }
};
double Value(uint16_t value) {
  return std::bit_cast<float>(static_cast<uint32_t>(value) << 16);
}
template <typename T>
std::vector<T> Read(const T* device, size_t elements) {
  std::vector<T> result(elements);
  RequireCuda(cudaMemcpy(result.data(), device, elements * sizeof(T),
                         cudaMemcpyDeviceToHost),
              "read observation");
  return result;
}
void Finite(const std::vector<uint16_t>& values, const std::string& label) {
  for (uint16_t value : values)
    if (!std::isfinite(Value(value)))
      throw std::runtime_error("non-finite BF16: " + label);
}
int32_t Argmax(const uint16_t* row, int vocab) {
  int32_t best = 0;
  for (int i = 0; i < vocab; ++i) {
    if (!std::isfinite(Value(row[i])))
      throw std::runtime_error("non-finite argmax input");
    if (Value(row[i]) > Value(row[best])) best = i;
  }
  return best;
}
// FNV is a compact label, not an integrity/equality oracle. Required state
// comparisons below compare every byte directly.
uint64_t Digest(const void* data, size_t bytes) {
  const auto* p = static_cast<const uint8_t*>(data);
  uint64_t result = UINT64_C(14695981039346656037);
  for (size_t i = 0; i < bytes; ++i) {
    result ^= p[i];
    result *= UINT64_C(1099511628211);
  }
  return result;
}
template <typename T>
bool Compare(const std::string& label, const std::vector<T>& actual,
             const std::vector<T>& expected) {
  Require(actual.size() == expected.size(), "shape mismatch: " + label);
  size_t unequal = 0, first = actual.size();
  for (size_t i = 0; i < actual.size(); ++i) {
    if (std::memcmp(&actual[i], &expected[i], sizeof(T)) == 0) continue;
    if (first == actual.size()) first = i;
    ++unequal;
  }
  std::printf(
      "  replay_compare=%s elements=%zu element_bytes=%zu "
      "unequal=%zu first=%zu actual_fnv64=%016llx "
      "expected_fnv64=%016llx\n",
      label.c_str(), actual.size(), sizeof(T), unequal, first,
      static_cast<unsigned long long>(
          Digest(actual.data(), actual.size() * sizeof(T))),
      static_cast<unsigned long long>(
          Digest(expected.data(), expected.size() * sizeof(T))));
  return unequal == 0;
}
class Evidence {
 public:
  explicit Evidence(const char* path) : dir_(path) {
    Require(std::filesystem::create_directory(dir_),
            "replay evidence directory must be new");
  }
  template <typename T>
  void Save(const std::string& name, const std::vector<T>& values) {
    const size_t bytes = values.size() * sizeof(T);
    Require(bytes < kArtifactLimit - bytes_, "replay artifact limit exceeded");
    std::ofstream file(dir_ / name, std::ios::binary | std::ios::out);
    file.exceptions(std::ios::failbit | std::ios::badbit);
    file.write(reinterpret_cast<const char*>(values.data()),
               static_cast<std::streamsize>(bytes));
    file.close();
    bytes_ += bytes;
  }
  size_t bytes() const { return bytes_; }

 private:
  std::filesystem::path dir_;
  size_t bytes_ = 0;
};

void ValidateSnapshot(const Model& model, const ModelStateSnapshot& state) {
  Require(state.valid && state.bytes == state.data.size() &&
              state.bytes == q4t::model::ModelStateSnapshotBytes(model),
          "snapshot identity/size mismatch");
  size_t offset = 0;
  const auto scan = [&](size_t elements, bool fp32) {
    const size_t width = fp32 ? sizeof(float) : sizeof(uint16_t);
    Require(offset + elements * width <= state.bytes, "snapshot span");
    for (size_t i = 0; i < elements; ++i) {
      double value;
      if (fp32) {
        float f;
        std::memcpy(&f, state.data.data() + offset + i * width, width);
        value = f;
      } else {
        uint16_t b;
        std::memcpy(&b, state.data.data() + offset + i * width, width);
        value = Value(b);
      }
      if (!std::isfinite(value))
        throw std::runtime_error("non-finite recurrent state");
    }
    offset += elements * width;
  };
  for (const auto& layer : model.layers) {
    if (layer.ssm_state) {
      scan(static_cast<size_t>(layer.linear.nv) * layer.linear.kd *
               layer.linear.vd,
           true);
      scan(static_cast<size_t>(layer.linear.in_qkv()) *
               (layer.linear.conv_k - 1),
           false);
    }
    if (layer.ple_conv_state)
      scan(static_cast<size_t>(layer.hc_dim) * (layer.ple.conv_kernel - 1) *
               layer.ple.conv_dilation,
           false);
  }
  Require(offset == state.bytes, "unparsed recurrent state");
}
ModelStateSnapshot Snapshot(const Model& model, const std::string& label) {
  ModelStateSnapshot result;
  RequireStatus(q4t::model::ModelSnapshotState(model, &result, nullptr),
                "snapshot main recurrent state");
  ValidateSnapshot(model, result);
  std::printf("  replay_state=%s bytes=%zu fnv64=%016llx finite=1\n",
              label.c_str(), result.bytes,
              static_cast<unsigned long long>(
                  Digest(result.data.data(), result.bytes)));
  return result;
}
void Restore(const Model& model, const ModelStateSnapshot& state) {
  Require(state.valid && state.bytes == state.data.size() &&
              state.bytes == q4t::model::ModelStateSnapshotBytes(model),
          "restore snapshot identity/size mismatch");
  RequireStatus(q4t::model::ModelRestoreState(model, state, nullptr),
                "restore main recurrent state");
}
// Use actual T-1 rows and cumulative layer spans, independently of the
// production restore reader. Host concatenation matches Snapshot.
ModelStateSnapshot RawCheckpoint(const Model& model, int row) {
  Require(model.cfg.max_seq == 1 && model.verify_ckpt_rows == kDrafts &&
              model.verify_ckpt_slots == std::vector<int>{0} && row >= 0 &&
              row < kDrafts,
          "raw checkpoint layout precondition");
  ModelStateSnapshot result;
  result.bytes = q4t::model::ModelStateSnapshotBytes(model);
  result.data.resize(result.bytes);
  size_t host = 0, ssm = 0, conv = 0, ple = 0;
  const auto copy = [&](const void* source, size_t bytes) {
    Require(host + bytes <= result.bytes, "raw checkpoint host span");
    RequireCuda(cudaMemcpy(result.data.data() + host, source, bytes,
                           cudaMemcpyDeviceToHost),
                "read raw checkpoint row");
    host += bytes;
  };
  for (const auto& layer : model.layers) {
    if (layer.ssm_state) {
      const size_t nssm = static_cast<size_t>(layer.linear.nv) *
                          layer.linear.kd * layer.linear.vd;
      const size_t nconv = static_cast<size_t>(layer.linear.in_qkv()) *
                           (layer.linear.conv_k - 1);
      copy(model.d_verify_ssm_ckpt + ssm + row * nssm, nssm * sizeof(float));
      copy(model.d_verify_conv_ckpt + conv + row * nconv,
           nconv * sizeof(uint16_t));
      ssm += kDrafts * nssm;
      conv += kDrafts * nconv;
    }
    if (layer.ple_conv_state) {
      const size_t nple = static_cast<size_t>(layer.hc_dim) *
                          (layer.ple.conv_kernel - 1) * layer.ple.conv_dilation;
      copy(model.d_verify_ple_conv_ckpt + ple + row * nple,
           nple * sizeof(uint16_t));
      ple += kDrafts * nple;
    }
  }
  Require(host == result.bytes, "raw checkpoint size mismatch");
  result.valid = true;
  ValidateSnapshot(model, result);
  return result;
}
void Verify(const Model& model, const ModelSequence& seq,
            const std::array<int32_t, kRows>& input, uint16_t* logits,
            uint16_t* trunk) {
  const int slot = 0;
  RequireStatus(q4t::model::ModelVerifyMulti(
                    model, input.data(), &seq.position, &slot,
                    seq.history.data(), static_cast<int>(seq.history.size()), 1,
                    kRows, logits, nullptr, trunk),
                "replay B1 T4 verify");
  RequireCuda(cudaStreamSynchronize(nullptr), "verify completion");
}
}  // namespace

Q4T_TEST(mtp_k3_full_model_replay) {
  const char* prompt_path = std::getenv("Q4T_MTP_REPLAY_PROMPT");
  const char* evidence_path = std::getenv("Q4T_MTP_REPLAY_DIR");
  Require(prompt_path && *prompt_path && evidence_path && *evidence_path,
          "set Q4T_MTP_REPLAY_PROMPT and new Q4T_MTP_REPLAY_DIR");
  for (const char* name : {"Q4T_GDN_REG", "Q4T_GDN_CHUNKED", "Q4T_GDN_SPLIT",
                           "Q4T_FP8_PROJ", "Q4T_FP8_HC", "Q4T_FP8_ALL",
                           "Q4T_MOE_STREAMS", "Q4T_LIN_DUMP", "Q4T_MLP_DUMP"})
    Require(std::getenv(name) == nullptr, std::string("unset ") + name);
  Evidence evidence(evidence_path);
  std::ifstream file(prompt_path, std::ios::binary);
  Require(file.good(), "cannot read frozen raw prompt");
  const std::string text((std::istreambuf_iterator<char>(file)),
                         std::istreambuf_iterator<char>());
  Require(!file.bad(), "frozen prompt read error");
  RequireCuda(cudaSetDevice(0), "CUDA required; no SKIP");
  q4t::model::ModelConfig cfg;
  cfg.model_dir =
      "/home/rm01/models/dev/llm/garnermccloud/"
      "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
  cfg.index_path = cfg.model_dir + "/model.safetensors.index.json";
  cfg.ple_sidecar = cfg.model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
  cfg.num_layers = 48;
  cfg.max_len = 208896;
  cfg.max_prefill = 8192;
  cfg.max_seq = 1;
  cfg.ple_capacity_tokens = 8192;
  std::unique_ptr<q4t::text::Tokenizer> tokenizer;
  RequireStatus(
      q4t::text::Tokenizer::Load(cfg.model_dir + "/tokenizer.json",
                                 q4t::text::TokenizerLimits{}, &tokenizer),
      "load tokenizer");
  std::vector<uint32_t> encoded;
  RequireStatus(tokenizer->Encode(text, &encoded), "encode raw prompt");
  Require(encoded.size() == 1024, "frozen raw prompt must encode to 1024");
  std::vector<int32_t> prompt(encoded.begin(), encoded.end());
  evidence.Save("prompt.txt", std::vector<char>(text.begin(), text.end()));
  evidence.Save("prompt.i32", prompt);
  std::printf(
      "MTP_REPLAY_SCOPE layers=48 max_len=208896 max_prefill=8192 "
      "max_seq=1 slot=0 k=3 prompt_tokens=1024 natural_steps=16 "
      "forced_steps=1 chat_wrap=0 recurrent_snapshot_only=1 "
      "performance_evidence=0 prompt_path=%s\n",
      prompt_path);
  std::fflush(stdout);
  q4t::model::ModelOwner owner;
  RequireStatus(owner.Load(cfg, nullptr), "load full main model");
  Model& model = owner.Get();
  MtpOwner draft;
  q4t::mtp::MtpConfig mcfg;
  mcfg.mtp_dir = cfg.model_dir + "/mtp";
  mcfg.max_len = cfg.max_len;
  mcfg.max_prefill = cfg.max_prefill;
  mcfg.max_seq = 1;
  RequireStatus(q4t::mtp::LoadMtp(mcfg, model.head.embed_tokens,
                                  model.head.lm_head, &draft.model, nullptr),
                "load actual MTP draft");
  auto& mtp = draft.model;
  RequireStatus(q4t::model::ModelReserveVerifyCheckpoints(model, kDrafts),
                "reserve k3 checkpoints");
  RequireStatus(q4t::mtp::MtpReserveScratch(mtp, kRows), "reserve k3 scratch");
  DeviceBuffer logits(static_cast<size_t>(kRows) * cfg.vocab);
  DeviceBuffer trunk(prompt.size() * model.hc_dim());
  DeviceBuffer g(model.hc_dim());
  ModelSequence seq;
  RequireStatus(q4t::model::ModelBeginSequence(model, &seq, nullptr, 0),
                "begin main sequence");
  RequireStatus(q4t::model::ModelPrefill(
                    model, &seq, prompt.data(), static_cast<int>(prompt.size()),
                    logits.data(), nullptr, trunk.data(), nullptr, 0,
                    q4t::model::LogitsRows::kLastRow),
                "prefill actual prompt");
  const auto prefill_logits = Read(logits.data(), cfg.vocab);
  Finite(prefill_logits, "prefill logits");
  const auto prefill_trunk = Read(trunk.data(), prompt.size() * model.hc_dim());
  Finite(prefill_trunk, "prefill trunk");
  int32_t bonus = Argmax(prefill_logits.data(), cfg.vocab), d0 = -1;
  RequireStatus(q4t::mtp::MtpResetState(mtp, nullptr, 0), "reset draft");
  std::vector<int32_t> shifted(prompt.begin() + 1, prompt.end());
  shifted.push_back(bonus);
  std::vector<int> positions(prompt.size());
  for (size_t i = 0; i < positions.size(); ++i) positions[i] = i;
  RequireStatus(q4t::mtp::MtpDraftExtend(
                    mtp, shifted.data(), trunk.data(), positions.data(),
                    static_cast<int>(prompt.size()), &d0, g.data(), nullptr, 0,
                    q4t::model::LogitsRows::kLastRow),
                "initial draft extend");
  RequireCuda(cudaStreamSynchronize(nullptr), "initial extend completion");
  bool contracts = true, all_rows_checked = true, forced_reject = false;
  std::array<int, kRows + 1> natural_counts{};
  std::vector<int32_t> emitted;
  for (int step = 0; step <= kNaturalSteps; ++step) {
    const bool forced = step == kNaturalSteps;
    const std::string label = "step" + std::to_string(step);
    const ModelSequence previous = seq;
    Require(seq.seq_id == 0 && seq.stage == ModelSequence::Stage::kDecode &&
                !seq.HasPending() &&
                seq.history.size() == static_cast<size_t>(seq.position),
            "main host sequence precondition");
    const auto before = Snapshot(model, label + ".before");
    int32_t seed = d0;
    if (forced) {
      std::printf(
          "MTP_REPLAY_FORCE_PROBE step=%d extra_main_verify=1 "
          "natural_candidate=0 tokens=%d,97,131,211\n",
          step, bonus);
      Verify(model, seq, {bonus, 97, 131, 211}, logits.data(), trunk.data());
      const auto probe =
          Read(logits.data(), static_cast<size_t>(kRows) * cfg.vocab);
      evidence.Save(label + ".force_probe_logits.bf16", probe);
      Finite(probe, "forced seed probe");
      const int32_t prediction = Argmax(probe.data(), cfg.vocab);
      seed = (prediction + 1) % cfg.vocab;
      std::printf(
          "MTP_REPLAY_FORCE_SEED step=%d probe_argmax=%d seed=%d "
          "suffix_dependence_not_assumed=1\n",
          step, prediction, seed);
      Restore(model, before);
    }
    std::printf(
        "MTP_REPLAY_ACTUAL step=%d mode=%s position=%d bonus=%d seed=%d\n",
        step, forced ? "forced_d0" : "natural", seq.position, bonus, seed);
    std::fflush(stdout);
    const ModelSequence* sequences[1] = {&seq};
    const uint16_t* input_g[1] = {g.data()};
    uint16_t* output_g[1] = {g.data()};
    std::array<int32_t, kRows> accepted{-1, -1, -1, -1};
    int count = 0;
    int32_t next = -1, next_d0 = -1;
    RequireStatus(
        q4t::mtp::MtpSpeculativeStepMulti(
            model, mtp, sequences, &bonus, &seed, input_g, 1, kDrafts,
            accepted.data(), &count, &next, &next_d0, output_g, nullptr),
        "actual k3 speculative step");
    RequireCuda(cudaStreamSynchronize(nullptr), "actual step completion");
    // Persist the first failure's raw observations before interpreting them.
    // Result layout: accepted[4], count, correction, next_d0, bonus, seed.
    evidence.Save(
        label + ".actual_result.i32",
        std::vector<int32_t>{accepted[0], accepted[1], accepted[2], accepted[3],
                             count, next, next_d0, bonus, seed});
    const bool host_unchanged =
        seq.stage == previous.stage && seq.position == previous.position &&
        seq.seq_id == previous.seq_id && seq.history == previous.history &&
        !seq.HasPending();
    contracts &= host_unchanged;
    const auto drafts = Read(mtp.d_ms_drafts, kDrafts);
    const auto target_logits =
        Read(mtp.d_ms_vlogits, static_cast<size_t>(kRows) * cfg.vocab);
    const auto target_trunk =
        Read(mtp.d_ms_vtrunk, static_cast<size_t>(kRows) * model.hc_dim());
    evidence.Save(label + ".drafts.i32", drafts);
    evidence.Save(label + ".verify_logits.bf16", target_logits);
    evidence.Save(label + ".verify_trunk.bf16", target_trunk);
    Require(count >= 1 && count <= kRows, "invalid actual accepted count");
    Require(host_unchanged, "Multi mutated caller sequence");
    Finite(target_logits, label + ".target_logits");
    Finite(target_trunk, label + ".target_trunk");
    for (int32_t token : drafts)
      Require(token >= 0 && token < cfg.vocab, "invalid actual draft token");
    std::array<int32_t, kRows> predictions;
    for (int row = 0; row < kRows; ++row)
      predictions[row] =
          Argmax(target_logits.data() + static_cast<size_t>(row) * cfg.vocab,
                 cfg.vocab);
    // Locate the first mismatch over entire vectors; do not reuse the
    // production host accept loop or its GPU argmax implementation.
    const auto mismatch =
        std::mismatch(drafts.begin(), drafts.end(), predictions.begin());
    const int accepted_drafts =
        static_cast<int>(std::distance(drafts.begin(), mismatch.first));
    const int expected_count = accepted_drafts + 1;
    const std::array<int32_t, kRows> inputs{bonus, drafts[0], drafts[1],
                                            drafts[2]};
    const bool selection =
        count == expected_count && next == predictions[accepted_drafts] &&
        std::equal(inputs.begin(), inputs.begin() + expected_count,
                   accepted.begin()) &&
        drafts[0] == seed;
    contracts &= selection;
    Require(selection, "actual selection differs from host target argmax");
    if (forced)
      forced_reject = count == 1;
    else
      ++natural_counts[count];
    const auto actual_after = Snapshot(model, label + ".actual_after");
    const auto extend_logits =
        Read(mtp.d_ms_ext_logits + static_cast<size_t>(count - 1) * cfg.vocab,
             cfg.vocab);
    const auto extend_g = Read(
        mtp.d_ms_ext_multi + static_cast<size_t>(count - 1) * model.hc_dim(),
        model.hc_dim());
    const auto next_g = Read(g.data(), model.hc_dim());
    evidence.Save(label + ".extend_last_logits.bf16", extend_logits);
    evidence.Save(label + ".extend_last_trunk.bf16", extend_g);
    evidence.Save(label + ".next_g.bf16", next_g);
    Finite(extend_logits, label + ".last_extend_logits");
    Finite(extend_g, label + ".last_extend_trunk");
    Finite(next_g, label + ".next_g");
    const bool next_seed = next_d0 == Argmax(extend_logits.data(), cfg.vocab);
    contracts &= next_seed;
    contracts &= Compare(label + ".next_g", next_g, extend_g);
    Require(next_seed, "next draft seed differs from extend argmax");
    Restore(model, before);
    std::printf("MTP_REPLAY_TARGET step=%d same_shape=B1T4 position=%d\n", step,
                previous.position);
    Verify(model, previous, inputs, logits.data(), trunk.data());
    const auto replay_logits = Read(logits.data(), target_logits.size());
    const auto replay_trunk = Read(trunk.data(), target_trunk.size());
    const bool logits_equal =
        Compare(label + ".target_logits", replay_logits, target_logits);
    const bool trunk_equal =
        Compare(label + ".target_trunk", replay_trunk, target_trunk);
    contracts &= logits_equal && trunk_equal;
    if (!logits_equal)
      evidence.Save(label + ".failed_replay_logits.bf16", replay_logits);
    if (!trunk_equal)
      evidence.Save(label + ".failed_replay_trunk.bf16", replay_trunk);
    Finite(replay_logits, label + ".replay_logits");
    Finite(replay_trunk, label + ".replay_trunk");
    Require(logits_equal && trunk_equal, "same-shape target replay differs");
    if (step == 0) {
      const auto full_after = Snapshot(model, label + ".replay_full_after");
      for (int row = 0; row < kDrafts; ++row) {
        const auto raw = RawCheckpoint(model, row);
        RequireStatus(
            q4t::model::ModelRestoreCheckpoint(model, row, nullptr, 0),
            "restore each independent raw checkpoint");
        const auto restored =
            Snapshot(model, label + ".row" + std::to_string(row));
        all_rows_checked &= Compare(label + ".raw_row" + std::to_string(row),
                                    restored.data, raw.data);
      }
      if (accepted_drafts == kDrafts) Restore(model, full_after);
    }
    if (accepted_drafts < kDrafts)
      RequireStatus(q4t::model::ModelRestoreCheckpoint(model, accepted_drafts,
                                                       nullptr, 0),
                    "restore actual accepted prefix");
    const auto replay_after = Snapshot(model, label + ".replay_after");
    const bool state_equal = Compare(label + ".accepted_state",
                                     replay_after.data, actual_after.data);
    contracts &= state_equal;
    Require(state_equal, "replayed accepted state differs");
    contracts &= Compare(label + ".draft_g_unchanged_by_main_replay",
                         Read(g.data(), model.hc_dim()), next_g);
    std::printf(
        "MTP_REPLAY_STEP step=%d mode=%s selection_exact=%d "
        "caller_unchanged=%d count=%d accepted_drafts=%d "
        "drafts=%d,%d,%d predictions=%d,%d,%d,%d correction=%d "
        "next_d0=%d replay_logits_exact=%d replay_trunk_exact=%d "
        "replay_state_exact=%d\n",
        step, forced ? "forced_d0" : "natural", selection, host_unchanged,
        count, accepted_drafts, drafts[0], drafts[1], drafts[2], predictions[0],
        predictions[1], predictions[2], predictions[3], next, next_d0,
        logits_equal, trunk_equal, state_equal);
    seq.position += count;
    seq.history.insert(seq.history.end(), accepted.begin(),
                       accepted.begin() + count);
    emitted.insert(emitted.end(), accepted.begin(), accepted.begin() + count);
    bonus = next;
    d0 = next_d0;
  }
  evidence.Save("accepted_tokens.i32", emitted);
  const bool coverage = natural_counts[kRows] > 0 && forced_reject;
  std::printf(
      "MTP_K3_REPLAY natural_steps=16 forced_steps=1 "
      "force_probe_extra_verify=1 actual_steps=17 replay_verifies=17 "
      "contracts=%d checkpoint_rows_exact=%d natural_count1=%d "
      "natural_count2=%d natural_count3=%d natural_count4=%d "
      "forced_reject0=%d coverage_complete=%d output_tokens=%zu "
      "artifact_bytes=%zu plain_equality_not_asserted=1\n",
      contracts, all_rows_checked, natural_counts[1], natural_counts[2],
      natural_counts[3], natural_counts[4], forced_reject, coverage,
      emitted.size(), evidence.bytes());
  return contracts && all_rows_checked && coverage;
}
