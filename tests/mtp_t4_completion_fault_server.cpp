// Link-only instrumentation for nine real T4 HTTP requests. Never link into
// normal q4t. Real operations execute first; only return values are injected.
// These are logical errors, not sticky CUDA faults, OOM or fake model math.
#include "q4t/mtp/mtp.h"

#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <span>
#include <thread>
#include <vector>

// These are external imports in scheduler/mtp objects, checked against the
// existing static library with nm. The final span suffix follows the proposed
// terminal patch ABI; confirm that import in the combined build before HTTP.
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
#define Q4T_RESET \
  "_ZN3q4t3mtp13MtpResetStateERKNS0_8MtpModelEP11CUstream_sti"

using q4t::Status;
using q4t::model::FullAttentionWeights;
using q4t::model::Model;
using q4t::model::ModelSequence;
using q4t::mtp::MtpModel;

Status RealStep(const Model&, const MtpModel&, const ModelSequence* const*,
                const int32_t*, const int32_t*, const uint16_t* const*, int, int,
                int32_t*, int*, int32_t*, int32_t*, uint16_t**, cudaStream_t,
                q4t::trace::MtpCycleStep*, q4t::trace::MtpVerifyMoeStep*,
                std::span<const int32_t>)
    asm("__real_" Q4T_STEP);
Status WrapStep(const Model&, const MtpModel&, const ModelSequence* const*,
                const int32_t*, const int32_t*, const uint16_t* const*, int, int,
                int32_t*, int*, int32_t*, int32_t*, uint16_t**, cudaStream_t,
                q4t::trace::MtpCycleStep*, q4t::trace::MtpVerifyMoeStep*,
                std::span<const int32_t>)
    asm("__wrap_" Q4T_STEP);
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
Status RealReset(const MtpModel&, cudaStream_t, int)
    asm("__real_" Q4T_RESET);
Status WrapReset(const MtpModel&, cudaStream_t, int)
    asm("__wrap_" Q4T_RESET);
extern "C" cudaError_t __real_cudaStreamSynchronize(cudaStream_t);
extern "C" cudaError_t __real_cudaMemcpyAsync(
    void*, const void*, size_t, cudaMemcpyKind, cudaStream_t);

namespace {

struct Observation {
  int ordinal = 0;
  ModelSequence* seq = nullptr;
  const Model* main = nullptr;
  const MtpModel* mtp = nullptr;
  bool in_step = false;
  bool in_verify = false;
  bool verify_completed = false;
  cudaStream_t stream = nullptr;
  std::thread::id owner;
  int steps = 0;
  int verifies = 0;
  int restores = 0;
  int extends = 0;
  int ordinary_calls = 0;
  int resets = 0;
  bool injected = false;
  int inner_drains = 0;
  int outer_drains = 0;
  bool outputs_checked = false;
  int step_position = 0;
  std::vector<int32_t> step_history;
};

std::mutex observer_mu;
Observation observed;

struct AnnounceVariant {
  AnnounceVariant() {
    std::fprintf(stderr,
                 "[q4t][t4_fault_variant] protocol=t4_completion_v1 "
                 "production_binary=0 requests=9\n");
  }
} announce_variant;

void Require(bool condition, const char* reason) {
  if (!condition) {
    std::fprintf(stderr, "[q4t][t4_fault_contract] ordinal=%d failure=%s\n",
                 observed.ordinal, reason);
    std::abort();
  }
}

bool Owner(cudaStream_t stream) {
  return observed.in_step && observed.stream == stream &&
         observed.owner == std::this_thread::get_id();
}

bool ExpectedFailure() {
  return observed.ordinal == 2 || observed.ordinal == 4 ||
         observed.ordinal == 6 || observed.ordinal == 8;
}

void Inject(cudaStream_t stream, const char* kind) {
  Require(Owner(stream) && !observed.injected, "duplicate_or_unowned_fault");
  observed.injected = true;
  std::fprintf(stderr,
               "[q4t][t4_fault_injection] ordinal=%d kind=%s real_ok=1 "
               "position=%d history=%zu pending=%d step=%d\n",
               observed.ordinal, kind, observed.seq->position,
               observed.seq->history.size(), observed.seq->HasPending(),
               observed.steps);
}

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
               "[q4t][t4_fault_begin] ordinal=%d real_ok=1 slot=0 "
               "stage=prefill position=0 history=0 pending=0\n",
               observed.ordinal);
  return status;
}

Status WrapReset(const MtpModel& model, cudaStream_t stream, int slot) {
  Status status = RealReset(model, stream, slot);
  const std::lock_guard<std::mutex> lock(observer_mu);
  if (!observed.seq) return status;  // Loading is outside a request lifetime.
  Require(slot == 0 && status.ok() && ++observed.resets == 1,
          "draft_reset_failed");
  observed.mtp = &model;
  std::fprintf(stderr,
               "[q4t][t4_fault_reset] ordinal=%d real_ok=1 slot=0 "
               "completion=stream_ordered\n", observed.ordinal);
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
    Require(observed.seq && observed.main == &model && observed.mtp == &mtp &&
                batch == 1 && k == 3 && seqs && seqs[0] == observed.seq &&
                !observed.in_step && !observed.injected && counts &&
                next_bonus && next_draft && !timing && !moe &&
                !observed.seq->HasPending(), "unexpected_t4_step");
    observed.in_step = true;
    observed.verify_completed = false;
    observed.stream = stream;
    observed.owner = std::this_thread::get_id();
    observed.step_position = observed.seq->position;
    observed.step_history = observed.seq->history;
    ++observed.steps;
    // Caller outputs begin nonempty so stale results cannot pass by accident.
    counts[0] = 313;
    next_bonus[0] = 314;
    next_draft[0] = 315;
  }
  Status status = RealStep(model, mtp, seqs, bonus, draft, hidden, batch, k,
                           accepted, counts, next_bonus, next_draft,
                           next_hidden, stream, timing, moe, stop_tokens);
  // Deliberately NO CUDA operation here. A test-side wait could hide a missing
  // production completion boundary before the step's local vectors expire.
  const std::lock_guard<std::mutex> lock(observer_mu);
  Require(Owner(stream) && !observed.in_verify, "step_ownership_lost");
  if (observed.injected) {
    Require(!status.ok() && observed.outer_drains >= 1 &&
                counts[0] == 0 && next_bonus[0] == -1 && next_draft[0] == -1 &&
                observed.seq->position == observed.step_position &&
                observed.seq->history == observed.step_history &&
                !observed.seq->HasPending(), "bad_failed_step_return");
    Require(observed.ordinal != 2 || observed.inner_drains >= 1,
            "verify_upload_returned_before_inner_drain");
    observed.outputs_checked = true;
    std::fprintf(stderr,
                 "[q4t][t4_fault_return] ordinal=%d real_ok=1 status=failed "
                 "counts=0 next_b=-1 next_d0=-1 caller_unchanged=1 "
                 "pending=0 observer_cuda_calls=0 inner_drains=%d "
                 "outer_drains=%d next_g=invalid_not_read\n",
                 observed.ordinal, observed.inner_drains, observed.outer_drains);
  } else {
    Require(status.ok() && counts[0] >= 1 && counts[0] <= 4,
            "unexpected_real_step_failure");
  }
  observed.in_step = false;
  return status;
}

Status WrapVerify(const Model& model, const int32_t* tokens,
                  const int* positions, const int* slots,
                  const int32_t* history, int history_len, int batch, int rows,
                  uint16_t* logits, cudaStream_t stream, uint16_t* trunk) {
  {
    const std::lock_guard<std::mutex> lock(observer_mu);
    Require(Owner(stream) && observed.main == &model && !observed.in_verify &&
                !observed.injected && batch == 1 && rows == 4 && slots &&
                slots[0] == 0 && logits == observed.mtp->d_ms_vlogits &&
                trunk == observed.mtp->d_ms_vtrunk, "unexpected_real_verify");
    observed.in_verify = true;
    ++observed.verifies;
  }
  Status status = RealVerify(model, tokens, positions, slots, history,
                             history_len, batch, rows, logits, stream, trunk);
  const std::lock_guard<std::mutex> lock(observer_mu);
  Require(Owner(stream) && observed.in_verify, "verify_ownership_lost");
  if (observed.injected) {
    Require(observed.ordinal == 2 && !status.ok() &&
                observed.inner_drains >= 1 && model.verify_ckpt_rows == 0 &&
                model.verify_ckpt_slots.empty(), "bad_failed_verify_return");
    std::fprintf(stderr,
                 "[q4t][t4_fault_verify_return] ordinal=2 real_ok=1 "
                 "status=failed inner_drained=1 checkpoint_rows=0 "
                 "checkpoint_slots=0 observer_cuda_calls=0\n");
  } else {
    Require(status.ok(), "unexpected_real_verify_failure");
    observed.verify_completed = true;
  }
  observed.in_verify = false;
  if (observed.ordinal == 4 && !observed.injected) {
    Inject(stream, "verify_return");
    return Status::Fail("HTTP T4 admission: real verify returned failure");
  }
  return status;
}

extern "C" cudaError_t __wrap_cudaMemcpyAsync(
    void* destination, const void* source, size_t bytes, cudaMemcpyKind kind,
    cudaStream_t stream) {
  const cudaError_t result = __real_cudaMemcpyAsync(
      destination, source, bytes, kind, stream);
  const std::lock_guard<std::mutex> lock(observer_mu);
  if (Owner(stream) && observed.in_verify && observed.ordinal == 2 &&
      !observed.injected && destination == observed.main->d_token_seq_id &&
      bytes == 4 * sizeof(int) && kind == cudaMemcpyHostToDevice) {
    Require(result == cudaSuccess, "real_sequence_id_upload_failed");
    Inject(stream, "verify_sequence_upload");
    // Returning this code does not set or clear the CUDA runtime's error state.
    return cudaErrorInvalidValue;
  }
  return result;
}

Status WrapRestore(const Model& model, int checkpoint, cudaStream_t stream,
                   int slot) {
  {
    const std::lock_guard<std::mutex> lock(observer_mu);
    Require(Owner(stream) && observed.main == &model &&
                observed.verify_completed && !observed.injected && slot == 0,
            "unexpected_restore");
    ++observed.restores;
  }
  Status status = RealRestore(model, checkpoint, stream, slot);
  const std::lock_guard<std::mutex> lock(observer_mu);
  Require(status.ok(), "real_restore_failed");
  if (observed.ordinal == 6) {
    Inject(stream, "natural_restore");
    return Status::Fail("HTTP T4 admission: real restore returned failure");
  }
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
    if (Owner(stream)) {
      Require(!observed.injected, "attention_after_injection");
      extend = observed.verify_completed && !observed.in_verify &&
               &weights == &observed.mtp->full_attn;
      if (extend) {
        Require(kv == observed.mtp->kv_cache && rows >= 1 && rows <= 4 &&
                    slots == observed.mtp->d_ms_ext_seq,
                "extend_attention_identity_mismatch");
        ++observed.extends;
      }
    }
  }
  Status status = RealAttention(weights, input, output, positions, rope, kv,
                                pages, raw, compressed, rows, workspace,
                                workspace_bytes, stream, slots, max_position);
  const std::lock_guard<std::mutex> lock(observer_mu);
  if (extend) {
    Require(status.ok(), "real_extend_attention_failed");
    if (observed.ordinal == 8) {
      Inject(stream, "extend_attention");
      return Status::Fail("HTTP T4 admission: real extend attention failed");
    }
  }
  return status;
}

extern "C" cudaError_t __wrap_cudaStreamSynchronize(cudaStream_t stream) {
  const cudaError_t result = __real_cudaStreamSynchronize(stream);
  const std::lock_guard<std::mutex> lock(observer_mu);
  if (Owner(stream) && observed.injected) {
    Require(result == cudaSuccess, "post_injection_drain_failed");
    if (observed.in_verify) ++observed.inner_drains;
    else ++observed.outer_drains;
    std::fprintf(stderr,
                 "[q4t][t4_fault_drain] ordinal=%d real_ok=1 scope=%s "
                 "after_injection=1 stream_match=1 thread_match=1\n",
                 observed.ordinal, observed.in_verify ? "verify" : "step");
  }
  return result;
}

Status WrapMain(const Model& model, const int32_t* tokens,
                const int* positions, const int* slots, const int32_t* history,
                int batch, uint16_t* logits, cudaStream_t stream,
                uint16_t* trunk) {
  {
    const std::lock_guard<std::mutex> lock(observer_mu);
    Require(observed.seq && !observed.injected,
            "ordinary_forward_after_failure_or_outside_request");
    ++observed.ordinary_calls;
  }
  return RealMain(model, tokens, positions, slots, history, batch, logits,
                  stream, trunk);
}

Status WrapEnd(ModelSequence* seq) {
  {
    const std::lock_guard<std::mutex> lock(observer_mu);
    Require(seq && seq == observed.seq && !observed.in_step &&
                !seq->HasPending() && observed.resets == 1 &&
                observed.steps >= 1, "bad_request_end");
    Require(observed.injected == ExpectedFailure(), "missing_planned_injection");
    if (ExpectedFailure()) {
      Require(observed.outputs_checked && observed.outer_drains >= 1 &&
                  seq->stage == ModelSequence::Stage::kFailed &&
                  seq->position == observed.step_position &&
                  seq->history == observed.step_history &&
                  observed.ordinary_calls == 0, "failed_state_not_preserved");
    }
  }
  Status status = RealEnd(seq);
  const std::lock_guard<std::mutex> lock(observer_mu);
  Require(status.ok() && seq->stage == ModelSequence::Stage::kIdle &&
              !seq->HasPending() && seq->position == 0 && seq->history.empty(),
          "host_sequence_not_cleared");
  std::fprintf(stderr,
               "[q4t][t4_fault_end] ordinal=%d real_ok=1 injected=%d "
               "outputs_checked=%d failed_before_end=%d stage=idle "
               "position=0 history=0 pending=0 steps=%d verifies=%d "
               "restores=%d extends=%d ordinary_calls=%d\n",
               observed.ordinal, observed.injected, observed.outputs_checked,
               ExpectedFailure(), observed.steps, observed.verifies,
               observed.restores, observed.extends, observed.ordinary_calls);
  observed.seq = nullptr;
  return status;
}
