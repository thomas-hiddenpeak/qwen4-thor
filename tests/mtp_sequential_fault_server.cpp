// Link-only HTTP admission instrumentation. Never linked into the normal q4t.
// Five serial requests: control, target-call-2 failure, recovery, first-extend
// failure, recovery. All forwards run for real; only their returned Status is
// changed. This does not simulate a sticky CUDA error, OOM or device failure.
#include "q4t/mtp/mtp.h"

#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <thread>
#include <vector>

#define Q4T_MAIN_FORWARD \
  "_ZN3q4t5model21ModelDecodeBatchMultiERKNS0_5ModelEPKiS5_S5_S5_iPt" \
  "P11CUstream_stS6_"
#define Q4T_MTP_FORWARD \
  "_ZN3q4t3mtp10MtpForwardERKNS0_8MtpModelEPKiS5_PKtPtS8_S8_i" \
  "P11CUstream_stS5_bNS_5model10LogitsRowsEPNS0_13MtpInitTimingEi" \
  "NS0_14MtpForwardModeE"
#define Q4T_BEGIN \
  "_ZN3q4t5model18ModelBeginSequenceERKNS0_5ModelEPNS0_13ModelSequenceE" \
  "P11CUstream_sti"
#define Q4T_END "_ZN3q4t5model16ModelEndSequenceEPNS0_13ModelSequenceE"
#define Q4T_RESET \
  "_ZN3q4t3mtp13MtpResetStateERKNS0_8MtpModelEP11CUstream_sti"

using q4t::Status;
using q4t::model::Model;
using q4t::model::ModelSequence;
using q4t::mtp::MtpModel;

Status RealMain(const Model&, const int32_t*, const int*, const int*,
                const int32_t*, int, uint16_t*, cudaStream_t, uint16_t*)
    asm("__real_" Q4T_MAIN_FORWARD);
Status WrapMain(const Model&, const int32_t*, const int*, const int*,
                const int32_t*, int, uint16_t*, cudaStream_t, uint16_t*)
    asm("__wrap_" Q4T_MAIN_FORWARD);
Status RealMtp(const MtpModel&, const int32_t*, const int*, const uint16_t*,
               uint16_t*, uint16_t*, uint16_t*, int, cudaStream_t, const int*,
               bool, q4t::model::LogitsRows, q4t::mtp::MtpInitTiming*, int,
               q4t::mtp::MtpForwardMode) asm("__real_" Q4T_MTP_FORWARD);
Status WrapMtp(const MtpModel&, const int32_t*, const int*, const uint16_t*,
               uint16_t*, uint16_t*, uint16_t*, int, cudaStream_t, const int*,
               bool, q4t::model::LogitsRows, q4t::mtp::MtpInitTiming*, int,
               q4t::mtp::MtpForwardMode) asm("__wrap_" Q4T_MTP_FORWARD);
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

namespace {

struct Observation {
  int ordinal = 0;
  ModelSequence* seq = nullptr;
  const Model* main = nullptr;
  const MtpModel* mtp = nullptr;
  int target_calls = 0;
  int draft_calls = 0;
  int extend_calls = 0;
  int tail_calls = 0;
  int resets = 0;
  bool injected = false;
  bool drained = false;
  cudaStream_t failed_stream = nullptr;
  std::thread::id failed_thread;
  int committed_position = 0;
  size_t committed_history = 0;
  std::vector<int32_t> committed_tokens;
};

std::mutex observer_mu;
Observation observed;

struct AnnounceVariant {
  AnnounceVariant() {
    std::fprintf(stderr,
                 "[q4t][fault_variant] protocol=sequential_recovery_v1 "
                 "production_binary=0 requests=5\n");
  }
} announce_variant;

void Require(bool ok, const char* reason) {
  if (!ok) {
    std::fprintf(stderr, "[q4t][fault_contract] ordinal=%d failure=%s\n",
                 observed.ordinal, reason);
    std::abort();
  }
}

void Inject(cudaStream_t stream, const char* kind) {
  Require(observed.seq && !observed.injected, "duplicate_or_unowned_fault");
  observed.injected = true;
  observed.failed_stream = stream;
  observed.failed_thread = std::this_thread::get_id();
  observed.committed_position = observed.seq->position;
  observed.committed_history = observed.seq->history.size();
  observed.committed_tokens = observed.seq->history;
  std::fprintf(stderr,
               "[q4t][fault_injection] ordinal=%d kind=%s real_ok=1 "
               "target_calls=%d draft_calls=%d extend_calls=%d "
               "position=%d history=%zu pending=%d\n",
               observed.ordinal, kind, observed.target_calls,
               observed.draft_calls, observed.extend_calls,
               observed.committed_position, observed.committed_history,
               observed.seq->HasPending());
}

}  // namespace

Status WrapBegin(const Model& model, ModelSequence* seq, cudaStream_t stream,
                 int slot) {
  {
    const std::lock_guard<std::mutex> lock(observer_mu);
    Require(!observed.seq && observed.ordinal < 5 && slot == 0 &&
                model.cfg.max_seq == 1,
            "unexpected_begin");
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
              seq->stage == ModelSequence::Stage::kPrefill,
          "main_reset_failed");
  std::fprintf(stderr,
               "[q4t][fault_begin] ordinal=%d real_ok=1 slot=0 "
               "stage=prefill position=0 history=0 pending=0\n",
               observed.ordinal);
  return status;
}

Status WrapReset(const MtpModel& model, cudaStream_t stream, int slot) {
  Status status = RealReset(model, stream, slot);
  const std::lock_guard<std::mutex> lock(observer_mu);
  Require(observed.seq && slot == 0 && status.ok() && ++observed.resets == 1,
          "draft_reset_failed");
  observed.mtp = &model;
  std::fprintf(stderr,
               "[q4t][fault_reset] ordinal=%d real_ok=1 slot=0 "
               "completion=stream_ordered\n", observed.ordinal);
  return status;
}

Status WrapMain(const Model& model, const int32_t* tokens,
                const int* positions, const int* slots, const int32_t* history,
                int batch, uint16_t* logits, cudaStream_t stream,
                uint16_t* trunk) {
  bool target = false;
  {
    const std::lock_guard<std::mutex> lock(observer_mu);
    Require(observed.seq && observed.main == &model && batch == 1 &&
                slots && slots[0] == 0 && !observed.injected,
            "main_after_failure_or_outside_request");
    const auto* mtp = observed.mtp;
    for (int row = 0; mtp && mtp->d_ms_vtrunk && mtp->d_ms_vlogits && row < 4;
         ++row) {
      target |= trunk == mtp->d_ms_vtrunk +
                             static_cast<size_t>(row) * mtp->hc_dim() &&
                logits == mtp->d_ms_vlogits +
                              static_cast<size_t>(row) * mtp->cfg.vocab;
    }
    Require(target || trunk == nullptr, "unclassified_main_forward");
    if (target) ++observed.target_calls;
    else ++observed.tail_calls;
  }
  Status status = RealMain(model, tokens, positions, slots, history, batch,
                           logits, stream, trunk);
  const std::lock_guard<std::mutex> lock(observer_mu);
  Require(status.ok(), "real_main_forward_failed");
  if (observed.ordinal == 2 && target && observed.target_calls == 2) {
    Inject(stream, "target_call_2_in_request");
    return Status::Fail("HTTP admission: real target call 2 returned failure");
  }
  return status;
}

Status WrapMtp(const MtpModel& model, const int32_t* ids, const int* positions,
               const uint16_t* hidden, uint16_t* sample, uint16_t* multi,
               uint16_t* logits, int rows, cudaStream_t stream,
               const int* slots, bool compute_logits,
               q4t::model::LogitsRows logits_rows,
               q4t::mtp::MtpInitTiming* timing, int max_position,
               q4t::mtp::MtpForwardMode mode) {
  bool extend = false;
  bool draft = false;
  {
    const std::lock_guard<std::mutex> lock(observer_mu);
    Require(observed.seq && observed.mtp == &model && !observed.injected,
            "mtp_after_failure_or_outside_request");
    extend = ids == model.d_ms_ext_ids && hidden == model.d_ms_vtrunk &&
             sample == model.d_ms_ext_sample &&
             multi == model.d_ms_ext_multi && logits == model.d_ms_ext_logits;
    draft = ids == model.d_ms_ids && hidden == model.d_ms_g_pool &&
            sample == model.d_ms_sample && multi == model.d_ms_multi &&
            logits == model.d_ms_vlogits && rows == 1;
    // Calls inside mtp.cu usually resolve within that object and bypass wrap.
    // If a linker does expose initialization calls, pass them through without
    // counting them as sequential attempts or ever injecting into them.
    const bool init = !extend && !draft && slots == nullptr;
    Require(init || ((extend || draft) && slots == model.d_ms_ext_seq &&
                compute_logits &&
                logits_rows == q4t::model::LogitsRows::kAllRows && !timing &&
                mode == q4t::mtp::MtpForwardMode::kFull &&
                rows >= 1 && rows <= 4),
            "unclassified_mtp_forward");
    if (extend) ++observed.extend_calls;
    else if (draft) ++observed.draft_calls;
  }
  Status status = RealMtp(model, ids, positions, hidden, sample, multi, logits,
                          rows, stream, slots, compute_logits, logits_rows,
                          timing, max_position, mode);
  const std::lock_guard<std::mutex> lock(observer_mu);
  Require(status.ok(), "real_mtp_forward_failed");
  if (observed.ordinal == 4 && extend && observed.extend_calls == 1) {
    Inject(stream, "first_extend");
    return Status::Fail("HTTP admission: real first extend returned failure");
  }
  return status;
}

extern "C" cudaError_t __wrap_cudaStreamSynchronize(cudaStream_t stream) {
  const cudaError_t result = __real_cudaStreamSynchronize(stream);
  const std::lock_guard<std::mutex> lock(observer_mu);
  if (observed.seq && observed.injected && !observed.drained &&
      stream == observed.failed_stream &&
      std::this_thread::get_id() == observed.failed_thread) {
    Require(result == cudaSuccess, "fault_drain_failed");
    observed.drained = true;
    std::fprintf(stderr,
                 "[q4t][fault_drain] ordinal=%d real_ok=1 "
                 "stream_match=1 thread_match=1\n",
                 observed.ordinal);
  }
  return result;
}

Status WrapEnd(ModelSequence* seq) {
  const std::lock_guard<std::mutex> lock(observer_mu);
  const bool expected_failure = observed.ordinal == 2 || observed.ordinal == 4;
  Require(seq && observed.seq == seq && !seq->HasPending() &&
              observed.resets == 1 && observed.injected == expected_failure,
          "end_missing_injection_or_owned_work");
  if (expected_failure) {
    Require(observed.drained && seq->stage == ModelSequence::Stage::kFailed &&
                seq->position == observed.committed_position &&
                seq->history.size() == observed.committed_history &&
                seq->history == observed.committed_tokens &&
                observed.tail_calls == 0,
            "failed_prefix_published_or_not_drained");
  }
  Status status = RealEnd(seq);
  Require(status.ok() && seq->stage == ModelSequence::Stage::kIdle &&
              !seq->HasPending() && seq->position == 0 && seq->history.empty(),
          "host_sequence_not_cleared");
  std::fprintf(stderr,
               "[q4t][fault_end] ordinal=%d real_ok=1 injected=%d "
               "drained=%d stage=idle position=0 history=0 pending=0 "
               "target_calls=%d draft_calls=%d extend_calls=%d tail_calls=%d\n",
               observed.ordinal, observed.injected, observed.drained,
               observed.target_calls, observed.draft_calls,
               observed.extend_calls, observed.tail_calls);
  observed.seq = nullptr;
  return status;
}
