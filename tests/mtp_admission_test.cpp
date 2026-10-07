// B=1 MTP admission diagnostics against the actual serve plain-decode entry.
// Four loaded layers cover linear attention, PLE and full attention. This is
// a bounded numerical/rollback contract, not an entire-model quality oracle.
// Cross-path mismatches remain failures pending explanation; no error budget
// is invented here. Run in a fresh process with the default GDN environment.
#include "q4t/model/model_owner.h"
#include "q4t/mtp/mtp.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <algorithm>
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

template <typename T>
class DeviceBuffer {
 public:
  explicit DeviceBuffer(size_t count) {
    RequireCuda(cudaMalloc(reinterpret_cast<void**>(&data_), count * sizeof(T)),
                "test buffer allocation");
  }
  ~DeviceBuffer() {
    const cudaError_t error = cudaFree(data_);
    if (error != cudaSuccess) {
      std::fprintf(stderr, "test buffer free: %s\n", cudaGetErrorString(error));
      std::abort();
    }
  }
  DeviceBuffer(const DeviceBuffer&) = delete;
  DeviceBuffer& operator=(const DeviceBuffer&) = delete;
  T* data() const { return data_; }

 private:
  T* data_ = nullptr;
};

template <typename T>
std::vector<T> Read(const T* device, size_t count) {
  std::vector<T> result(count);
  RequireCuda(cudaMemcpy(result.data(), device, count * sizeof(T),
                         cudaMemcpyDeviceToHost),
              "read test observation");
  return result;
}

float Value(uint16_t value) {
  const uint32_t bits = static_cast<uint32_t>(value) << 16;
  float result;
  std::memcpy(&result, &bits, sizeof(result));
  return result;
}
float Value(float value) { return value; }

template <typename T>
bool Compare(const std::string& label, const std::vector<T>& actual,
             const std::vector<T>& expected) {
  if (actual.size() != expected.size()) return false;
  size_t unequal = 0;
  bool finite = true;
  double squared = 0, reference_squared = 0, max_abs = 0;
  for (size_t i = 0; i < actual.size(); ++i) {
    unequal += std::memcmp(&actual[i], &expected[i], sizeof(T)) != 0;
    const double a = Value(actual[i]), b = Value(expected[i]);
    finite = finite && std::isfinite(a) && std::isfinite(b);
    squared += (a - b) * (a - b);
    reference_squared += b * b;
    max_abs = std::max(max_abs, std::abs(a - b));
  }
  const double relative_l2 = reference_squared > 0
                                 ? std::sqrt(squared / reference_squared)
                                 : std::sqrt(squared);
  std::printf(
      "  compare=%s elements=%zu unequal=%zu finite=%d "
      "max_abs=%.9g relative_l2=%.9g\n",
      label.c_str(), actual.size(), unequal, finite, max_abs, relative_l2);
  return finite && unequal == 0;
}

int Argmax(const std::vector<uint16_t>& logits) {
  int result = 0;
  for (size_t i = 0; i < logits.size(); ++i) {
    if (!std::isfinite(Value(logits[i])))
      throw std::runtime_error("non-finite logits");
    if (Value(logits[i]) > Value(logits[result])) result = static_cast<int>(i);
  }
  return result;
}

struct RecurrentState {
  std::vector<std::vector<float>> ssm;
  std::vector<std::vector<uint16_t>> conv;
  std::vector<std::vector<uint16_t>> ple;
};

RecurrentState Capture(const Model& model) {
  RecurrentState result;
  for (const auto& layer : model.layers) {
    if (layer.ssm_state) {
      result.ssm.push_back(
          Read(layer.ssm_state, static_cast<size_t>(layer.linear.nv) *
                                    layer.linear.kd * layer.linear.vd));
      result.conv.push_back(
          Read(layer.conv_state, static_cast<size_t>(layer.linear.in_qkv()) *
                                     (layer.linear.conv_k - 1)));
    }
    if (layer.ple_conv_state) {
      result.ple.push_back(
          Read(layer.ple_conv_state, static_cast<size_t>(layer.hc_dim) *
                                         (layer.ple.conv_kernel - 1) *
                                         layer.ple.conv_dilation));
    }
  }
  return result;
}

bool CompareState(const std::string& label, const RecurrentState& actual,
                  const RecurrentState& expected) {
  bool equal = actual.ssm.size() == expected.ssm.size() &&
               actual.conv.size() == expected.conv.size() &&
               actual.ple.size() == expected.ple.size();
  if (!equal) return false;
  for (size_t i = 0; i < actual.ssm.size(); ++i)
    equal &= Compare(label + ".ssm" + std::to_string(i), actual.ssm[i],
                     expected.ssm[i]);
  for (size_t i = 0; i < actual.conv.size(); ++i)
    equal &= Compare(label + ".conv" + std::to_string(i), actual.conv[i],
                     expected.conv[i]);
  for (size_t i = 0; i < actual.ple.size(); ++i)
    equal &= Compare(label + ".ple" + std::to_string(i), actual.ple[i],
                     expected.ple[i]);
  return equal;
}

void Prefill(const Model& model, const std::vector<int32_t>& prompt,
             ModelSequence* seq, uint16_t* logits, uint16_t* trunk) {
  Require(q4t::model::ModelBeginSequence(model, seq, nullptr, 0), "begin");
  Require(q4t::model::ModelPrefill(model, seq, prompt.data(),
                                   static_cast<int>(prompt.size()), logits,
                                   nullptr, trunk, nullptr, 0,
                                   q4t::model::LogitsRows::kLastRow),
          "prefill");
}

void Advance(ModelSequence* seq, const int32_t* tokens, int count) {
  seq->position += count;
  seq->history.insert(seq->history.end(), tokens, tokens + count);
}

// Exactly the scheduler's plain B=1 call, with the same PLE history window.
// It does not use re-prefill as a surrogate decode oracle.
void PlainStep(const Model& model, ModelSequence* seq, int32_t token,
               uint16_t* logits, uint16_t* trunk = nullptr) {
  const int width = model.ple_hash.ngram_size - 1;
  std::vector<int32_t> history(width, model.cfg.eos_token_id);
  for (int i = 0; i < width; ++i) {
    const int position = seq->position - width + i;
    if (position >= 0) history[i] = seq->history.at(position);
  }
  const int slot = 0;
  Require(q4t::model::ModelDecodeBatchMulti(model, &token, &seq->position,
                                            &slot, history.data(), 1, logits,
                                            nullptr, trunk),
          "plain B=1");
  RequireCuda(cudaStreamSynchronize(nullptr), "plain completion");
  Advance(seq, &token, 1);
}

void Verify(const Model& model, const ModelSequence& seq,
            const std::vector<int32_t>& tokens, uint16_t* logits,
            uint16_t* trunk = nullptr) {
  const int slot = 0;
  Require(q4t::model::ModelVerifyMulti(
              model, tokens.data(), &seq.position, &slot, seq.history.data(),
              static_cast<int>(seq.history.size()), 1,
              static_cast<int>(tokens.size()), logits, nullptr, trunk),
          "verify B=1");
  RequireCuda(cudaStreamSynchronize(nullptr), "verify completion");
}

struct MtpOwner {
  q4t::mtp::MtpModel model;
  ~MtpOwner() { model.Free(); }
};

void InitDraft(const q4t::mtp::MtpModel& mtp,
               const std::vector<int32_t>& prompt, int32_t bonus,
               const uint16_t* trunk, int32_t* d0, uint16_t* g) {
  Require(q4t::mtp::MtpResetState(mtp, nullptr, 0), "reset draft");
  std::vector<int32_t> shifted(prompt.begin() + 1, prompt.end());
  shifted.push_back(bonus);
  std::vector<int> positions(prompt.size());
  for (size_t i = 0; i < positions.size(); ++i) positions[i] = i;
  Require(q4t::mtp::MtpDraftExtend(mtp, shifted.data(), trunk, positions.data(),
                                   static_cast<int>(prompt.size()), d0, g,
                                   nullptr, 0),
          "draft extend");
  RequireCuda(cudaStreamSynchronize(nullptr), "draft extend completion");
}

}  // namespace

Q4T_TEST(mtp_admission_single_stream) {
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
  MtpOwner draft;
  q4t::mtp::MtpConfig mcfg;
  mcfg.mtp_dir = cfg.model_dir + "/mtp";
  mcfg.max_len = cfg.max_len;
  mcfg.max_prefill = cfg.max_prefill;
  Require(q4t::mtp::LoadMtp(mcfg, model.head.embed_tokens, model.head.lm_head,
                            &draft.model, nullptr),
          "load draft");
  auto& mtp = draft.model;
  Require(q4t::model::ModelReserveVerifyCheckpoints(model, 3), "checkpoints");
  Require(q4t::mtp::MtpReserveScratch(mtp, 4), "scratch");

  std::vector<int32_t> prompt;
  for (int i = 0; i < 13; ++i) prompt.push_back(42 + 17 * i);
  const int vocab = cfg.vocab, hc_dim = model.hc_dim();
  DeviceBuffer<uint16_t> logits(static_cast<size_t>(4) * vocab);
  DeviceBuffer<uint16_t> trunk(static_cast<size_t>(prompt.size()) * hc_dim);
  DeviceBuffer<uint16_t> g(hc_dim);
  bool numeric_equal = true, rollback_exact = true, greedy_equal = true;

  // Fixed future tokens isolate execution-path arithmetic from free-running
  // token divergence. Retain all first differences as diagnostics.
  const std::vector<int32_t> fixed = {97, 131, 211, 307};
  ModelSequence plain;
  Prefill(model, prompt, &plain, logits.data(), trunk.data());
  std::vector<std::vector<uint16_t>> plain_rows;
  for (int32_t token : fixed) {
    PlainStep(model, &plain, token, logits.data());
    plain_rows.push_back(Read(logits.data(), vocab));
  }
  const RecurrentState plain_state = Capture(model);
  ModelSequence verified;
  Prefill(model, prompt, &verified, logits.data(), trunk.data());
  Verify(model, verified, fixed, logits.data());
  for (size_t i = 0; i < fixed.size(); ++i) {
    const auto row = Read(logits.data() + i * vocab, vocab);
    numeric_equal &=
        Compare("fixed_logits" + std::to_string(i), row, plain_rows[i]);
    const int actual = Argmax(row), expected = Argmax(plain_rows[i]);
    greedy_equal &= actual == expected;
    std::printf("  fixed_row=%zu verify_argmax=%d plain_argmax=%d\n", i, actual,
                expected);
  }
  numeric_equal &= CompareState("fixed_state", Capture(model), plain_state);

  // k=1 makes both branch outcomes controllable through the public d0 input.
  // A same-path verify reference isolates rollback correctness even when the
  // cross-path arithmetic diagnostic above has already failed.
  for (bool accept : {false, true}) {
    ModelSequence seq;
    Prefill(model, prompt, &seq, logits.data(), trunk.data());
    const int32_t bonus = Argmax(Read(logits.data(), vocab));
    Verify(model, seq, {bonus, 97}, logits.data());
    const int32_t prediction = Argmax(Read(logits.data(), vocab));
    const int32_t forced_d0 = accept ? prediction : (prediction + 1) % vocab;
    Prefill(model, prompt, &seq, logits.data(), trunk.data());
    Verify(model, seq, {bonus, forced_d0}, logits.data());
    const int expected_count = accept ? 2 : 1;
    const int32_t expected_next = Argmax(
        Read(logits.data() + static_cast<size_t>(expected_count - 1) * vocab,
             vocab));
    if (!accept) {
      Require(q4t::model::ModelRestoreCheckpoint(model, 0, nullptr, 0),
              "reference rollback");
      RequireCuda(cudaStreamSynchronize(nullptr), "reference rollback drain");
    }
    const RecurrentState expected_state = Capture(model);
    const int32_t inputs[2] = {bonus, forced_d0};
    Advance(&seq, inputs, expected_count);
    PlainStep(model, &seq, 503, logits.data());
    const auto expected_probe = Read(logits.data(), vocab);

    Prefill(model, prompt, &seq, logits.data(), trunk.data());
    int32_t unused_d0 = -1;
    InitDraft(mtp, prompt, bonus, trunk.data(), &unused_d0, g.data());
    const ModelSequence* seqs[1] = {&seq};
    const uint16_t* input_g[1] = {g.data()};
    uint16_t* output_g[1] = {g.data()};
    int32_t accepted[2] = {-1, -1}, next = -1, next_d0 = -1;
    int count = 0;
    Require(q4t::mtp::MtpSpeculativeStepMulti(
                model, mtp, seqs, &bonus, &forced_d0, input_g, 1, 1, accepted,
                &count, &next, &next_d0, output_g, nullptr),
            "forced single-stream speculative step");
    RequireCuda(cudaStreamSynchronize(nullptr), "speculative completion");
    Q4T_CHECK(seq.position == static_cast<int>(prompt.size()));
    Q4T_CHECK(seq.history == prompt);
    Q4T_CHECK(count == expected_count && accepted[0] == bonus);
    Q4T_CHECK(!accept || accepted[1] == forced_d0);
    Q4T_CHECK(next == expected_next && next_d0 >= 0 && next_d0 < vocab);
    const std::string label = accept ? "accept" : "reject";
    rollback_exact &=
        CompareState(label + "_state", Capture(model), expected_state);
    Advance(&seq, accepted, count);
    PlainStep(model, &seq, 503, logits.data());
    rollback_exact &= Compare(label + "_next_fixed_logits",
                              Read(logits.data(), vocab), expected_probe);
  }

  // Actual k=3 repeated Multi calls, with no forced drafts. Build the plain
  // continuation once using formal decode and compare each accepted prefix
  // and correction token, even after earlier floating-point diagnostics fail.
  Prefill(model, prompt, &plain, logits.data(), trunk.data());
  std::vector<int32_t> expected_tokens;
  int32_t token = Argmax(Read(logits.data(), vocab));
  for (int i = 0; i < 13; ++i) {
    expected_tokens.push_back(token);
    PlainStep(model, &plain, token, logits.data());
    token = Argmax(Read(logits.data(), vocab));
  }
  ModelSequence seq;
  Prefill(model, prompt, &seq, logits.data(), trunk.data());
  int32_t bonus = Argmax(Read(logits.data(), vocab)), d0 = -1;
  InitDraft(mtp, prompt, bonus, trunk.data(), &d0, g.data());
  size_t offset = 0;
  for (int step = 0; step < 3; ++step) {
    const ModelSequence* seqs[1] = {&seq};
    const uint16_t* input_g[1] = {g.data()};
    uint16_t* output_g[1] = {g.data()};
    int32_t accepted[4] = {-1, -1, -1, -1};
    int count = 0;
    int32_t next = -1, next_d0 = -1;
    Require(q4t::mtp::MtpSpeculativeStepMulti(
                model, mtp, seqs, &bonus, &d0, input_g, 1, 3, accepted, &count,
                &next, &next_d0, output_g, nullptr),
            "natural single-stream speculative step");
    RequireCuda(cudaStreamSynchronize(nullptr), "natural step completion");
    Q4T_CHECK(count >= 1 && count <= 4 &&
              offset + count < expected_tokens.size());
    for (int i = 0; i < count; ++i) {
      const int32_t expected = expected_tokens[offset + i];
      greedy_equal &= accepted[i] == expected;
      std::printf("  step=%d row=%d actual=%d plain=%d\n", step, i, accepted[i],
                  expected);
    }
    offset += count;
    greedy_equal &= next == expected_tokens[offset];
    std::printf("  step=%d accepted=%d correction=%d plain_correction=%d\n",
                step, count, next, expected_tokens[offset]);
    Advance(&seq, accepted, count);
    bonus = next;
    d0 = next_d0;
  }
  std::printf(
      "MTP_ADMISSION numeric_equal=%d rollback_exact=%d greedy_equal=%d "
      "layers=4 B=1 k=3 default_gdn=1\n",
      numeric_equal, rollback_exact, greedy_equal);
  return numeric_equal && rollback_exact && greedy_equal;
}
