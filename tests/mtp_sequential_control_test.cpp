// Links the real mtp_sequential.cpp with CPU dependency stubs, never CUDA or
// model libraries. These tests establish control/ownership, not GPU arithmetic.
#include "q4t/mtp/mtp.h"
#include "q4t/model/model_head.h"
#include "q4t/test.h"

#include <algorithm>
#include <array>
#include <bit>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
constexpr int kVocab = 64, kHidden = 2, kTrunk = 4, kPosition = 2;
constexpr int32_t kBonus = 19;

void Require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

struct Harness {
  q4t::model::Model main;
  q4t::mtp::MtpModel mtp;
  q4t::model::ModelSequence seq;
  std::array<int32_t, 4> predictions{20, 21, 22, 23};
  std::array<int32_t, 3> drafts{20, 21, 22};
  std::array<int32_t, 2> stops{60, 61};
  std::array<uint16_t, kTrunk> seed{1, 2, 3, 4}, next_seed{};
  std::array<uint16_t, 4 * kVocab> target_logits{}, extend_logits{};
  std::array<uint16_t, 4 * kTrunk> target_trunk{}, extend_trunk{};
  std::array<uint16_t, 4 * kHidden> extend_sample{};
  std::array<uint16_t, kTrunk> rolling{}, draft_trunk{};
  std::array<uint16_t, kHidden> draft_sample{};
  std::array<int32_t, 4> ids{}, extend_ids{}, device_drafts{};
  std::array<int, 4> slots{};
  std::vector<int32_t> consumed;
  int target_calls = 0, draft_calls = 0, extend_calls = 0;
  int target_argmax = 0, draft_argmax = 0, copies = 0, drains = 0;
  int target_mutations = 0, draft_mutations = 0;
  int drains_at_failure = -1;
  int fail_target = 0;
  bool fail_extend = false;

  Harness() {
    main.cfg.hs = mtp.cfg.hs = kHidden;
    main.cfg.hc = mtp.cfg.hc = 2;
    main.cfg.vocab = mtp.cfg.vocab = kVocab;
    main.cfg.max_len = mtp.cfg.max_len = 64;
    main.cfg.max_prefill = mtp.cfg.max_prefill = 4;
    main.cfg.max_seq = mtp.cfg.max_seq = mtp.max_seq = 1;
    main.cfg.eos_token_id = 63;
    // The production host TU only checks this borrowed pointer for presence.
    // No fake object is dereferenced, constructed, freed or passed to CUDA.
    main.ple_emb = reinterpret_cast<q4t::ple::PleEmbedding*>(uintptr_t{1});
    main.ple_hash.ngram_size = 4;
    seq.position = kPosition;
    seq.history = {11, 12};
    seq.stage = q4t::model::ModelSequence::Stage::kDecode;
    mtp.k_max = 4;
    mtp.d_ms_vlogits = target_logits.data();
    mtp.d_ms_vtrunk = target_trunk.data();
    mtp.d_ms_ext_logits = extend_logits.data();
    mtp.d_ms_ext_multi = extend_trunk.data();
    mtp.d_ms_ext_sample = extend_sample.data();
    mtp.d_ms_ids = ids.data();
    mtp.d_ms_ext_ids = extend_ids.data();
    mtp.d_ms_drafts = device_drafts.data();
    mtp.d_ms_ext_seq = slots.data();
    mtp.d_ms_g_pool = rolling.data();
    mtp.d_ms_multi = draft_trunk.data();
    mtp.d_ms_sample = draft_sample.data();
  }

  void CheckCaller() const {
    Require(seq.position == kPosition &&
                seq.history == std::vector<int32_t>({11, 12}) &&
                seq.seq_id == 0 &&
                seq.stage == q4t::model::ModelSequence::Stage::kDecode &&
                !seq.HasPending(),
            "production driver changed caller state");
  }
};
Harness* active = nullptr;

uint16_t TrunkValue(int row, int column) {
  return static_cast<uint16_t>(0x3f00 + row * kTrunk + column);
}
void Logits(uint16_t* logits, int32_t prediction) {
  std::fill_n(logits, kVocab, uint16_t{0});
  logits[prediction] = 0x3f80;
}
q4t::Status Reduce(const uint16_t* logits, int rows, int vocab,
                   int32_t* output) {
  Require(rows == 1 && vocab == kVocab, "unexpected reduction shape");
  int32_t best = 0;
  for (int i = 1; i < vocab; ++i) {
    const float value = std::bit_cast<float>(uint32_t{logits[i]} << 16);
    const float prior = std::bit_cast<float>(uint32_t{logits[best]} << 16);
    if (value > prior) best = i;
  }
  output[0] = best;
  return {};
}
q4t::Status Run(Harness& harness, q4t::mtp::MtpSequentialResult* result,
                int32_t bonus = kBonus) {
  active = &harness;
  const auto status = q4t::mtp::MtpSpeculativeStepSequentialTarget(
      harness.main, harness.mtp, harness.seq, bonus, harness.drafts[0],
      harness.seed.data(), 16, harness.stops, result, harness.next_seed.data(),
      nullptr);
  harness.CheckCaller();
  active = nullptr;
  return status;
}
void CheckPrefix(const Harness& harness,
                 const q4t::mtp::MtpSequentialResult& result, int rows) {
  Require(result.accepted_count == rows &&
              result.target_forward_calls == rows &&
              result.draft_forward_calls == 2 && harness.target_calls == rows &&
              harness.target_argmax == rows && harness.draft_calls == 2 &&
              harness.consumed.size() == static_cast<size_t>(rows),
          "incorrect target/proposal counts");
  for (int i = 0; i < rows; ++i) {
    const int32_t expected = i == 0 ? kBonus : harness.predictions[i - 1];
    Require(result.accepted_tokens[i] == expected &&
                harness.consumed[i] == expected,
            "incorrect consumed prefix");
  }
  Require(result.next_b == harness.predictions[rows - 1],
          "incorrect pending correction");
}
}  // namespace

// No real CUDA runtime is linked to this executable. All buffers are owned
// host arrays and the production TU's copy/drain calls are observable here.
extern "C" cudaError_t CUDARTAPI cudaMemcpyAsync(void* destination,
                                                 const void* source,
                                                 size_t bytes, cudaMemcpyKind,
                                                 cudaStream_t stream) {
  Require(active && stream == nullptr, "unexpected copy context");
  ++active->copies;
  std::memmove(destination, source, bytes);
  return cudaSuccess;
}
extern "C" cudaError_t CUDARTAPI cudaStreamSynchronize(cudaStream_t stream) {
  Require(active && stream == nullptr, "unexpected drain context");
  ++active->drains;
  return cudaSuccess;
}
extern "C" const char* CUDARTAPI cudaGetErrorString(cudaError_t) {
  return "controlled CUDA error";
}

namespace q4t::model {
Status ModelDecodeBatchMulti(const Model& model, const int32_t* tokens,
                             const int* positions, const int* seq_ids,
                             const int32_t* history, int batch,
                             uint16_t* logits, cudaStream_t stream,
                             uint16_t* trunk) {
  auto& h = *active;
  const int row = h.target_calls++;
  Require(&model == &h.main && stream == nullptr && batch == 1 &&
              seq_ids[0] == 0 && row < 4 && positions[0] == kPosition + row,
          "target did not use ordinary B1 arguments");
  const int32_t expected = row == 0 ? kBonus : h.predictions[row - 1];
  Require(tokens[0] == expected && std::find(h.stops.begin(), h.stops.end(),
                                             tokens[0]) == h.stops.end(),
          "target consumed rejected/stop token");
  std::vector<int32_t> prefix = h.seq.history;
  prefix.insert(prefix.end(), h.consumed.begin(), h.consumed.end());
  for (int i = 0; i < 3; ++i) {
    const int source = static_cast<int>(prefix.size()) - 3 + i;
    Require(history[i] == (source < 0 ? 63 : prefix[source]),
            "target PLE history is not ordinary oldest-first prefix");
  }
  ++h.target_mutations;
  h.consumed.push_back(tokens[0]);
  if (h.fail_target == row + 1) {
    h.drains_at_failure = h.drains;
    return Status::Fail("injected target failure");
  }
  Logits(logits, h.predictions[row]);
  for (int i = 0; i < kTrunk; ++i) trunk[i] = TrunkValue(row, i);
  return {};
}
Status ArgmaxBf16Rows(const uint16_t* logits, int rows, int vocab,
                      int32_t* output, cudaStream_t stream) {
  Require(active && stream == nullptr, "unexpected target reduction");
  ++active->target_argmax;
  return Reduce(logits, rows, vocab, output);
}
}  // namespace q4t::model

namespace q4t::mtp {
Status MtpForward(const MtpModel& mtp_model, const int32_t* input_ids,
                  const int* positions, const uint16_t* hidden,
                  uint16_t* sample, uint16_t* multi, uint16_t* logits, int rows,
                  cudaStream_t stream, const int* seq_ids, bool compute_logits,
                  model::LogitsRows logits_rows, MtpInitTiming* timing,
                  int maximum, MtpForwardMode mode) {
  auto& h = *active;
  Require(&mtp_model == &h.mtp && stream == nullptr && seq_ids &&
              compute_logits && logits_rows == model::LogitsRows::kAllRows &&
              !timing && mode == MtpForwardMode::kFull,
          "incorrect pooled MTP forward contract");
  ++h.draft_mutations;
  if (h.draft_calls < 2) {
    const int step = h.draft_calls++;
    Require(rows == 1 && positions[0] == kPosition + step &&
                maximum == positions[0] && input_ids[0] == h.drafts[step] &&
                seq_ids[0] == 0,
            "incorrect real proposal input/position");
    for (int i = 0; i < kTrunk; ++i) {
      const uint16_t expected = step == 0 ? h.seed[i] : uint16_t(100 + i);
      Require(hidden[i] == expected, "proposal rolling hidden was lost");
      multi[i] = static_cast<uint16_t>(100 + step * kTrunk + i);
    }
    std::fill_n(sample, kHidden, uint16_t{1});
    Logits(logits, h.drafts[step + 1]);
    return {};
  }
  ++h.extend_calls;
  Require(rows == h.target_calls && rows >= 1 && rows <= 4 &&
              maximum == kPosition + rows - 1,
          "extend count/max position mismatch");
  for (int row = 0; row < rows; ++row) {
    Require(input_ids[row] == h.predictions[row] &&
                positions[row] == kPosition + row && seq_ids[row] == 0,
            "extend EAGLE shift/position mismatch");
    for (int i = 0; i < kTrunk; ++i)
      Require(hidden[row * kTrunk + i] == TrunkValue(row, i),
              "extend used wrong target trunk");
  }
  if (h.fail_extend) {
    h.drains_at_failure = h.drains;
    return Status::Fail("injected extend failure");
  }
  for (int row = 0; row < rows; ++row) {
    Logits(logits + row * kVocab, 30 + row);
    for (int i = 0; i < kTrunk; ++i)
      multi[row * kTrunk + i] = static_cast<uint16_t>(200 + row * kTrunk + i);
  }
  std::fill_n(sample, rows * kHidden, uint16_t{2});
  return {};
}
Status ArgmaxBf16Rows(const uint16_t* logits, int rows, int vocab,
                      int32_t* output, cudaStream_t stream) {
  Require(active && stream == nullptr, "unexpected draft reduction");
  ++active->draft_argmax;
  return Reduce(logits, rows, vocab, output);
}
}  // namespace q4t::mtp

Q4T_TEST(mtp_sequential_control_acceptance) {
  for (int accepted = 0; accepted <= 3; ++accepted) {
    Harness h;
    if (accepted < 3) h.drafts[accepted] = h.predictions[accepted] + 1;
    q4t::mtp::MtpSequentialResult result;
    Require(Run(h, &result).ok(), "complete sequential step failed");
    const int rows = accepted + 1;
    CheckPrefix(h, result, rows);
    Require(!result.terminal && result.next_seed_valid &&
                result.next_d0 == 30 + rows - 1 &&
                result.extend_forward_calls == 1 && h.extend_calls == 1 &&
                h.draft_argmax == 3 && h.drains > 0,
            "extend/next seed contract failed");
    for (int i = 0; i < kTrunk; ++i)
      Require(h.next_seed[i] == 200 + (rows - 1) * kTrunk + i,
              "next hidden is not final extend row");
  }
  return true;
}

Q4T_TEST(mtp_sequential_control_stop) {
  {
    Harness h;
    q4t::mtp::MtpSequentialResult result;
    Require(!Run(h, &result, h.stops[0]).ok(), "bonus stop was accepted");
    Require(h.copies == 0 && h.drains == 0 && h.target_calls == 0 &&
                h.draft_calls == 0 && result.accepted_count == 0,
            "bonus stop submitted device work");
  }
  for (int row = 0; row < 4; ++row) {
    const int variants = row < 3 ? 2 : 1;
    for (int variant = 0; variant < variants; ++variant) {
      Harness h;
      h.predictions[row] = h.stops[row % 2];
      if (row < 3) h.drafts[row] = variant == 0 ? h.predictions[row] : 40;
      q4t::mtp::MtpSequentialResult result;
      Require(Run(h, &result).ok(), "terminal sequential step failed");
      CheckPrefix(h, result, row + 1);
      Require(result.terminal && !result.next_seed_valid &&
                  result.next_d0 == -1 && result.extend_forward_calls == 0 &&
                  h.extend_calls == 0 && h.draft_argmax == 2,
              "terminal result extended or published a seed");
      // Request-level emission has one extra terminal token, whereas committed
      // history contains precisely the consumed non-stop prefix above.
      std::vector<int32_t> emitted = h.consumed;
      emitted.push_back(result.next_b);
      Require(emitted.size() == h.consumed.size() + 1 &&
                  std::find(h.stops.begin(), h.stops.end(), emitted.back()) !=
                      h.stops.end(),
              "terminal output/consumption boundary");
    }
  }
  return true;
}

Q4T_TEST(mtp_sequential_control_partial_failure) {
  for (int failure = 0; failure < 2; ++failure) {
    Harness h;
    h.fail_target = failure == 0 ? 2 : 0;
    h.fail_extend = failure == 1;
    q4t::mtp::MtpSequentialResult result;
    result.accepted_count = 4;
    result.next_b = result.next_d0 = 7;
    result.next_seed_valid = result.terminal = true;
    Require(!Run(h, &result).ok(), "injected failure reported success");
    Require(h.target_mutations == (failure == 0 ? 2 : 4) &&
                h.target_calls == h.target_mutations &&
                h.drains_at_failure >= 0 && h.drains > h.drains_at_failure &&
                result.draft_forward_calls == 2 &&
                result.target_forward_calls == h.target_calls &&
                result.extend_forward_calls == failure &&
                result.accepted_count == 0 && result.next_b == -1 &&
                result.next_d0 == -1 && !result.terminal &&
                !result.next_seed_valid &&
                result.accepted_tokens == std::array<int32_t, 4>{},
            "failed partial step left publishable output or resumed target");
  }
  return true;
}
