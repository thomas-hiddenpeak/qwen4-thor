// Link-only terminal selection control. Real draft, T4 verify and restore run.
// Only host verify argmax selections change, after the actual D2H succeeds.
// This is not natural generation quality or numerical admission evidence.
#include "q4t/mtp/mtp.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <span>
#include <thread>
#include <vector>

#define Q4T_STEP \
  "_ZN3q4t3mtp23MtpSpeculativeStepMultiERKNS_5model5ModelERKNS0_8MtpModelE" \
  "PKPKNS1_13ModelSequenceEPKiSE_PKPKtiiPiSJ_SJ_SJ_PPtP11CUstream_st" \
  "PNS_5trace12MtpCycleStepEPNSO_16MtpVerifyMoeStepE" \
  "St4spanISD_Lm18446744073709551615EE"
#define Q4T_VERIFY \
  "_ZN3q4t5model16ModelVerifyMultiERKNS0_5ModelEPKiS5_S5_S5_iiiPt" \
  "P11CUstream_stS6_"
#define Q4T_RESTORE \
  "_ZN3q4t5model22ModelRestoreCheckpointERKNS0_5ModelEiP11CUstream_sti"
#define Q4T_ATTENTION \
  "_ZN3q4t5model20FullAttentionForwardERKNS0_20FullAttentionWeightsEPKtPt" \
  "PKiS8_S6_S8_S6_S6_iPvmP11CUstream_stS8_i"
#define Q4T_MAIN \
  "_ZN3q4t5model21ModelDecodeBatchMultiERKNS0_5ModelEPKiS5_S5_S5_iPt" \
  "P11CUstream_stS6_"
#define Q4T_BEGIN \
  "_ZN3q4t5model18ModelBeginSequenceERKNS0_5ModelEPNS0_13ModelSequenceE" \
  "P11CUstream_sti"
#define Q4T_END "_ZN3q4t5model16ModelEndSequenceEPNS0_13ModelSequenceE"
#define Q4T_PREFILL \
  "_ZN3q4t5model12ModelPrefillERKNS0_5ModelEPNS0_13ModelSequenceEPKiiPt" \
  "P11CUstream_stS8_PKNS0_14VisionFeaturesEiNS0_10LogitsRowsE" \
  "NS0_18SequenceCompletionE"

using q4t::Status;
using q4t::model::FullAttentionWeights;
using q4t::model::Model;
using q4t::model::ModelSequence;
using q4t::mtp::MtpModel;

Status RealStep(const Model&, const MtpModel&, const ModelSequence* const*,
                const int32_t*, const int32_t*, const uint16_t* const*, int, int,
                int32_t*, int*, int32_t*, int32_t*, uint16_t**, cudaStream_t,
                q4t::trace::MtpCycleStep*, q4t::trace::MtpVerifyMoeStep*,
                std::span<const int32_t>) asm("__real_" Q4T_STEP);
Status WrapStep(const Model&, const MtpModel&, const ModelSequence* const*,
                const int32_t*, const int32_t*, const uint16_t* const*, int, int,
                int32_t*, int*, int32_t*, int32_t*, uint16_t**, cudaStream_t,
                q4t::trace::MtpCycleStep*, q4t::trace::MtpVerifyMoeStep*,
                std::span<const int32_t>) asm("__wrap_" Q4T_STEP);
Status RealVerify(const Model&, const int32_t*, const int*, const int*,
                  const int32_t*, int, int, int, uint16_t*, cudaStream_t,
                  uint16_t*) asm("__real_" Q4T_VERIFY);
Status WrapVerify(const Model&, const int32_t*, const int*, const int*,
                  const int32_t*, int, int, int, uint16_t*, cudaStream_t,
                  uint16_t*) asm("__wrap_" Q4T_VERIFY);
Status RealRestore(const Model&, int, cudaStream_t, int)
    asm("__real_" Q4T_RESTORE);
Status WrapRestore(const Model&, int, cudaStream_t, int)
    asm("__wrap_" Q4T_RESTORE);
Status RealAttention(const FullAttentionWeights&, const uint16_t*, uint16_t*,
                     const int*, const int*, uint16_t*, const int*, uint16_t*,
                     uint16_t*, int, void*, size_t, cudaStream_t, const int*, int)
    asm("__real_" Q4T_ATTENTION);
Status WrapAttention(const FullAttentionWeights&, const uint16_t*, uint16_t*,
                     const int*, const int*, uint16_t*, const int*, uint16_t*,
                     uint16_t*, int, void*, size_t, cudaStream_t, const int*, int)
    asm("__wrap_" Q4T_ATTENTION);
Status RealMain(const Model&, const int32_t*, const int*, const int*,
                const int32_t*, int, uint16_t*, cudaStream_t, uint16_t*)
    asm("__real_" Q4T_MAIN);
Status WrapMain(const Model&, const int32_t*, const int*, const int*,
                const int32_t*, int, uint16_t*, cudaStream_t, uint16_t*)
    asm("__wrap_" Q4T_MAIN);
Status RealBegin(const Model&, ModelSequence*, cudaStream_t, int)
    asm("__real_" Q4T_BEGIN);
Status WrapBegin(const Model&, ModelSequence*, cudaStream_t, int)
    asm("__wrap_" Q4T_BEGIN);
Status RealEnd(ModelSequence*) asm("__real_" Q4T_END);
Status WrapEnd(ModelSequence*) asm("__wrap_" Q4T_END);
Status RealPrefill(const Model&, ModelSequence*, const int32_t*, int, uint16_t*,
                   cudaStream_t, uint16_t*, const q4t::model::VisionFeatures*,
                   int, q4t::model::LogitsRows, q4t::model::SequenceCompletion)
    asm("__real_" Q4T_PREFILL);
Status WrapPrefill(const Model&, ModelSequence*, const int32_t*, int, uint16_t*,
                   cudaStream_t, uint16_t*, const q4t::model::VisionFeatures*,
                   int, q4t::model::LogitsRows, q4t::model::SequenceCompletion)
    asm("__wrap_" Q4T_PREFILL);
extern "C" cudaError_t __real_cudaMemcpy(
    void*, const void*, size_t, cudaMemcpyKind);

namespace {

struct Observation {
  int ordinal = 0;
  ModelSequence* seq = nullptr;
  const Model* main = nullptr;
  const MtpModel* mtp = nullptr;
  bool in_step = false;
  bool in_verify = false;
  bool verified = false;
  bool draft_read = false;
  bool selected = false;
  bool returned = false;
  cudaStream_t stream = nullptr;
  std::thread::id owner;
  int steps = 0;
  int restores = 0;
  int extends = 0;
  int ordinary = 0;
  int bonus = -1;
  int next_bonus = -1;
  std::array<int32_t, 3> drafts{};
  std::array<int32_t, 4> original{};
  std::array<int32_t, 4> chosen{};
  std::vector<int32_t> stop;
  std::vector<int32_t> prompt;
  std::vector<int32_t> history;
  std::vector<int32_t> committed;
  std::vector<int32_t> ordinary_tokens;
};

std::mutex observer_mu;
Observation observed;

struct AnnounceVariant {
  AnnounceVariant() {
    std::fprintf(stderr,
                 "[q4t][t4_terminal_variant] protocol=t4_terminal_v1 "
                 "production_binary=0 requests=9 selection_control=1\n");
  }
} announce_variant;

void Require(bool condition, const char* reason) {
  if (!condition) {
    std::fprintf(stderr,
                 "[q4t][t4_terminal_contract] ordinal=%d failure=%s\n",
                 observed.ordinal, reason);
    std::abort();
  }
}

bool Owner() {
  return observed.in_step && observed.owner == std::this_thread::get_id();
}

bool IsStop(int token) {
  return std::find(observed.stop.begin(), observed.stop.end(), token) !=
         observed.stop.end();
}

int StopRow() { return observed.ordinal <= 4 ? observed.ordinal - 1 : -1; }

}  // namespace

Status WrapBegin(const Model& model, ModelSequence* seq, cudaStream_t stream,
                 int slot) {
  {
    const std::lock_guard<std::mutex> lock(observer_mu);
    Require(!observed.seq && observed.ordinal < 9 && slot == 0 &&
                model.cfg.max_seq == 1, "unexpected_begin");
    const int ordinal = observed.ordinal + 1;
    observed = Observation{};
    observed.ordinal = ordinal;
    observed.seq = seq;
    observed.main = &model;
  }
  Status status = RealBegin(model, seq, stream, slot);
  const std::lock_guard<std::mutex> lock(observer_mu);
  Require(status.ok() && seq && !seq->HasPending() && seq->position == 0 &&
              seq->history.empty() &&
              seq->stage == ModelSequence::Stage::kPrefill, "begin_not_clean");
  std::fprintf(stderr,
               "[q4t][t4_terminal_begin] ordinal=%d real_ok=1 slot=0 "
               "stage=prefill position=0 history=0 pending=0\n",
               observed.ordinal);
  return status;
}

Status WrapPrefill(const Model& model, ModelSequence* seq,
                   const int32_t* tokens, int rows, uint16_t* logits,
                   cudaStream_t stream, uint16_t* trunk,
                   const q4t::model::VisionFeatures* vision, int slot,
                   q4t::model::LogitsRows logits_rows,
                   q4t::model::SequenceCompletion completion) {
  {
    const std::lock_guard<std::mutex> lock(observer_mu);
    Require(observed.main == &model && observed.seq == seq && tokens &&
                rows == 1024 && !vision && slot == 0 &&
                observed.prompt.empty(), "unexpected_prefill_input");
    observed.prompt.assign(tokens, tokens + rows);
  }
  Status status = RealPrefill(model, seq, tokens, rows, logits, stream, trunk,
                              vision, slot, logits_rows, completion);
  const std::lock_guard<std::mutex> lock(observer_mu);
  Require(status.ok(), "real_prefill_failed");
  std::fprintf(stderr,
               "[q4t][t4_terminal_prefill] ordinal=%d real_ok=1 "
               "rows=1024 prompt_captured=1 completion=caller_owned\n",
               observed.ordinal);
  return status;
}

Status WrapStep(const Model& model, const MtpModel& mtp,
                const ModelSequence* const* seqs, const int32_t* bonus,
                const int32_t* draft, const uint16_t* const* hidden,
                int batch, int k, int32_t* accepted, int* counts,
                int32_t* next_bonus, int32_t* next_draft, uint16_t** next_hidden,
                cudaStream_t stream, q4t::trace::MtpCycleStep* timing,
                q4t::trace::MtpVerifyMoeStep* moe,
                std::span<const int32_t> stop_tokens) {
  {
    const std::lock_guard<std::mutex> lock(observer_mu);
    Require((observed.ordinal <= 4 || observed.ordinal == 9) &&
                observed.seq && observed.main == &model && batch == 1 &&
                k == 3 && seqs && seqs[0] == observed.seq && bonus &&
                !observed.in_step && observed.steps == 0 && !timing && !moe &&
                !stop_tokens.empty() && !observed.seq->HasPending() &&
                observed.seq->position == 1024 &&
                observed.prompt.size() == 1024 &&
                observed.seq->history == observed.prompt,
            "unexpected_t4_step");
    observed.mtp = &mtp;
    observed.in_step = true;
    observed.stream = stream;
    observed.owner = std::this_thread::get_id();
    observed.bonus = bonus[0];
    observed.stop.assign(stop_tokens.begin(), stop_tokens.end());
    Require(!IsStop(bonus[0]) && bonus[0] >= 0 && bonus[0] < model.cfg.vocab,
            "prefill_stop_or_invalid_bonus_coverage_missing");
    observed.history = observed.seq->history;
    ++observed.steps;
  }
  Status status = RealStep(model, mtp, seqs, bonus, draft, hidden, batch, k,
                           accepted, counts, next_bonus, next_draft,
                           next_hidden, stream, timing, moe, stop_tokens);
  // No observer CUDA operation: the production completion boundary stands.
  const std::lock_guard<std::mutex> lock(observer_mu);
  const int stop_row = StopRow();
  const int consumed = stop_row >= 0 ? stop_row + 1 : 4;
  Require(Owner() && status.ok() && observed.selected && observed.verified &&
              !observed.in_verify && counts[0] == consumed &&
              next_bonus[0] == observed.chosen[consumed - 1] &&
              observed.seq->position == 1024 &&
              observed.seq->history == observed.history &&
              !observed.seq->HasPending(), "bad_step_return");
  observed.committed.assign(accepted, accepted + consumed);
  Require(accepted[0] == observed.bonus &&
              std::equal(accepted + 1, accepted + consumed,
                         observed.drafts.begin()) &&
              std::none_of(observed.committed.begin(),
                           observed.committed.end(), IsStop),
          "bad_committed_prefix_or_stop_consumed");
  Require(observed.restores == (stop_row >= 0 && stop_row < 3 ? 1 : 0) &&
              observed.extends == (stop_row >= 0 ? 0 : 1) &&
              (stop_row >= 0 ? next_draft[0] == -1
                             : next_draft[0] >= 0 &&
                                   next_draft[0] < model.cfg.vocab),
          "bad_restore_extend_or_seed");
  observed.next_bonus = next_bonus[0];
  observed.returned = true;
  std::fprintf(stderr,
               "[q4t][t4_terminal_return] ordinal=%d real_ok=1 "
               "counts=%d next_b=%d next_d0=%d terminal=%d "
               "caller_unchanged=1 prefix_nonstop=1 pending=0 "
               "restores=%d extends=%d observer_cuda_calls=0 "
               "next_g=%s\n",
               observed.ordinal, consumed, next_bonus[0], next_draft[0],
               stop_row >= 0, observed.restores, observed.extends,
               stop_row >= 0 ? "invalid_not_read" : "valid_not_inspected");
  observed.in_step = false;
  return status;
}

Status WrapVerify(const Model& model, const int32_t* tokens,
                  const int* positions, const int* slots,
                  const int32_t* history, int history_len, int batch, int rows,
                  uint16_t* logits, cudaStream_t stream, uint16_t* trunk) {
  {
    const std::lock_guard<std::mutex> lock(observer_mu);
    Require(Owner() && observed.stream == stream && observed.draft_read &&
                observed.main == &model && !observed.verified &&
                !observed.in_verify && batch == 1 && rows == 4 && slots &&
                slots[0] == 0 && positions[0] == 1024 &&
                logits == observed.mtp->d_ms_vlogits &&
                trunk == observed.mtp->d_ms_vtrunk &&
                tokens[0] == observed.bonus &&
                std::equal(tokens + 1, tokens + 4, observed.drafts.begin()),
            "unexpected_real_verify");
    observed.in_verify = true;
  }
  Status status = RealVerify(model, tokens, positions, slots, history,
                             history_len, batch, rows, logits, stream, trunk);
  const std::lock_guard<std::mutex> lock(observer_mu);
  Require(Owner() && status.ok() && observed.in_verify,
          "real_verify_failed_or_ownership_lost");
  observed.in_verify = false;
  observed.verified = true;
  std::fprintf(stderr,
               "[q4t][t4_terminal_verify] ordinal=%d real_ok=1 "
               "batch=1 rows=4 position=1024 selected=0\n",
               observed.ordinal);
  return status;
}

extern "C" cudaError_t __wrap_cudaMemcpy(
    void* destination, const void* source, size_t bytes, cudaMemcpyKind kind) {
  const cudaError_t result =
      __real_cudaMemcpy(destination, source, bytes, kind);
  const std::lock_guard<std::mutex> lock(observer_mu);
  if (!Owner() || kind != cudaMemcpyDeviceToHost ||
      source != observed.mtp->d_ms_ext_ids) return result;
  Require(result == cudaSuccess, "real_control_readback_failed");
  if (!observed.verified && !observed.in_verify &&
      bytes == 3 * sizeof(int32_t)) {
    Require(!observed.draft_read, "duplicate_draft_matrix");
    std::copy_n(static_cast<const int32_t*>(destination), 3,
                observed.drafts.begin());
    observed.draft_read = true;
  } else if (observed.verified && !observed.in_verify &&
             bytes == 4 * sizeof(int32_t)) {
    Require(observed.draft_read && !observed.selected,
            "duplicate_or_unbound_verify_selection");
    auto* values = static_cast<int32_t*>(destination);
    std::copy_n(values, 4, observed.original.begin());
    const int stop_row = StopRow();
    const int prefix = stop_row >= 0 ? stop_row : 3;
    for (int index = 0; index < prefix; ++index) {
      Require(observed.drafts[index] >= 0 &&
                  observed.drafts[index] < observed.main->cfg.vocab &&
                  !IsStop(observed.drafts[index]),
              "earlier_actual_draft_stop_coverage_missing");
      values[index] = observed.drafts[index];
    }
    // For cap=5 use the already validated non-stop bonus as correction.
    // Its selection is deterministic and retained beside the real argmax.
    values[prefix] = stop_row >= 0 ? observed.stop[0] : observed.bonus;
    std::copy_n(values, 4, observed.chosen.begin());
    observed.selected = true;
    std::fprintf(stderr,
                 "[q4t][t4_terminal_select] ordinal=%d real_ok=1 "
                 "stop_row=%d stop_id=%d stop_count=%zu bonus=%d "
                 "draft=%d,%d,%d original=%d,%d,%d,%d "
                 "selected=%d,%d,%d,%d changed_after_real_d2h=1\n",
                 observed.ordinal, stop_row, observed.stop[0],
                 observed.stop.size(), observed.bonus, observed.drafts[0],
                 observed.drafts[1], observed.drafts[2], observed.original[0],
                 observed.original[1], observed.original[2],
                 observed.original[3], values[0], values[1], values[2],
                 values[3]);
  }
  return result;
}

Status WrapRestore(const Model& model, int checkpoint, cudaStream_t stream,
                   int slot) {
  {
    const std::lock_guard<std::mutex> lock(observer_mu);
    Require(Owner() && observed.stream == stream && observed.selected &&
                observed.main == &model && slot == 0 && StopRow() >= 0 &&
                StopRow() < 3 && checkpoint == StopRow() &&
                ++observed.restores == 1, "unexpected_checkpoint_restore");
  }
  Status status = RealRestore(model, checkpoint, stream, slot);
  const std::lock_guard<std::mutex> lock(observer_mu);
  Require(status.ok(), "real_restore_failed");
  std::fprintf(stderr,
               "[q4t][t4_terminal_restore] ordinal=%d real_ok=1 "
               "checkpoint=%d slot=0\n", observed.ordinal, checkpoint);
  return status;
}

Status WrapAttention(const FullAttentionWeights& weights, const uint16_t* input,
                     uint16_t* output, const int* positions, const int* rope,
                     uint16_t* kv, const int* pages, uint16_t* raw,
                     uint16_t* compressed, int rows, void* workspace,
                     size_t workspace_bytes, cudaStream_t stream,
                     const int* slots, int max_position) {
  bool extend = false;
  {
    const std::lock_guard<std::mutex> lock(observer_mu);
    extend = Owner() && observed.stream == stream && observed.verified &&
             !observed.in_verify && &weights == &observed.mtp->full_attn;
    if (extend) {
      Require(observed.ordinal == 9 && observed.selected && rows == 4 &&
                  kv == observed.mtp->kv_cache &&
                  slots == observed.mtp->d_ms_ext_seq &&
                  ++observed.extends == 1, "terminal_extend_or_bad_cap5");
    }
  }
  Status status = RealAttention(weights, input, output, positions, rope, kv,
                                pages, raw, compressed, rows, workspace,
                                workspace_bytes, stream, slots, max_position);
  const std::lock_guard<std::mutex> lock(observer_mu);
  if (extend) {
    Require(status.ok(), "real_extend_attention_failed");
    std::fprintf(stderr,
                 "[q4t][t4_terminal_extend] ordinal=9 real_ok=1 rows=4\n");
  }
  return status;
}

Status WrapMain(const Model& model, const int32_t* tokens,
                const int* positions, const int* slots, const int32_t* history,
                int batch, uint16_t* logits, cudaStream_t stream,
                uint16_t* trunk) {
  {
    const std::lock_guard<std::mutex> lock(observer_mu);
    Require(observed.seq && observed.ordinal >= 6 && observed.ordinal <= 8 &&
                observed.main == &model && batch == 1 && slots[0] == 0 &&
                observed.steps == 0 &&
                positions[0] == 1024 + observed.ordinary,
            "unexpected_ordinary_tail_forward");
    if (observed.ordinary == 0) {
      Require(observed.seq->history == observed.prompt &&
                  observed.prompt.size() == 1024, "prompt_history_changed");
      observed.history = observed.seq->history;
    }
    observed.ordinary_tokens.push_back(tokens[0]);
    ++observed.ordinary;
  }
  Status status = RealMain(model, tokens, positions, slots, history, batch,
                           logits, stream, trunk);
  const std::lock_guard<std::mutex> lock(observer_mu);
  Require(status.ok(), "real_ordinary_tail_failed");
  return status;
}

Status WrapEnd(ModelSequence* seq) {
  int position = 0;
  {
    const std::lock_guard<std::mutex> lock(observer_mu);
    const int consumed = observed.ordinal <= 4 ? observed.ordinal
                                               : observed.ordinal - 5;
    Require(seq && seq == observed.seq && !observed.in_step &&
                !seq->HasPending() &&
                seq->stage == ModelSequence::Stage::kDecode &&
                observed.prompt.size() == 1024 &&
                seq->position == 1024 + consumed &&
                seq->history.size() == static_cast<size_t>(1024 + consumed),
            "unexpected_committed_end_position");
    position = seq->position;
    if (observed.ordinal <= 4 || observed.ordinal == 9) {
      auto expected = observed.prompt;
      expected.insert(expected.end(), observed.committed.begin(),
                      observed.committed.end());
      Require(observed.steps == 1 && observed.returned && observed.selected &&
                  observed.ordinary == 0 && seq->history == expected,
              "terminal_history_or_extra_forward");
    } else {
      Require(observed.steps == 0 && !observed.selected &&
                  observed.ordinary == consumed, "pure_tail_not_covered");
      auto expected = observed.prompt;
      expected.insert(expected.end(), observed.ordinary_tokens.begin(),
                      observed.ordinary_tokens.end());
      Require(seq->history == expected, "ordinary_tail_history_changed");
    }
  }
  Status status = RealEnd(seq);
  const std::lock_guard<std::mutex> lock(observer_mu);
  Require(status.ok() && seq->stage == ModelSequence::Stage::kIdle &&
              !seq->HasPending() && seq->position == 0 && seq->history.empty(),
          "host_sequence_not_cleared");
  std::fprintf(stderr,
               "[q4t][t4_terminal_end] ordinal=%d real_ok=1 "
               "committed_position=%d committed_history=%d history_exact=1 "
               "steps=%d restores=%d extends=%d ordinary_calls=%d "
               "stage=idle position=0 history=0 pending=0\n",
               observed.ordinal, position, position, observed.steps,
               observed.restores, observed.extends, observed.ordinary);
  observed.seq = nullptr;
  return status;
}
