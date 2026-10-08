// Strict S1 target verification through the ordinary scheduler arithmetic.
// Separate host control TU so contract tests can link the real implementation
// with bounded dependency stubs; production links the normal CUDA primitives.
#include "q4t/mtp/mtp.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <vector>

#include "q4t/model/model_head.h"

namespace q4t::mtp {

// Existing draft/extend reduction in mtp.cu. Target verification deliberately
// uses model::ArgmaxBf16Rows, exactly like the ordinary decode scheduler.
Status ArgmaxBf16Rows(const uint16_t* logits, int rows, int vocab, int32_t* out,
                     cudaStream_t stream);

namespace {

bool IsSequentialStop(int32_t token, std::span<const int32_t> stop_tokens) {
  return std::find(stop_tokens.begin(), stop_tokens.end(), token) !=
         stop_tokens.end();
}

Status CheckSequentialInput(const model::Model& main, const MtpModel& mtp,
                            const model::ModelSequence& seq, int32_t b,
                            int output_remaining,
                            std::span<const int32_t> stop_tokens) {
  if (main.cfg.max_seq != 1 || mtp.max_seq != 1 || mtp.cfg.max_seq != 1 ||
      seq.seq_id != 0)
    return Status::Fail("MTP sequential: requires S1/slot0");
  if (seq.stage != model::ModelSequence::Stage::kDecode || seq.HasPending() ||
      seq.position < 0 ||
      seq.history.size() != static_cast<size_t>(seq.position))
    return Status::Fail("MTP sequential: invalid committed decode state");
  if (output_remaining <= 4 || seq.position > main.cfg.max_len ||
      main.cfg.max_len - seq.position <= 4 ||
      seq.position > mtp.cfg.max_len || mtp.cfg.max_len - seq.position <= 4)
    return Status::Fail("MTP sequential: complete k3 step does not fit");
  if (main.cfg.vocab <= 0 || mtp.cfg.vocab != main.cfg.vocab ||
      main.cfg.hs <= 0 || mtp.cfg.hs != main.cfg.hs || main.cfg.hc <= 0 ||
      mtp.cfg.hc != main.cfg.hc || main.cfg.max_prefill < 1 ||
      mtp.cfg.max_prefill < 4 ||
      (main.ple_emb && main.ple_hash.ngram_size <= 0))
    return Status::Fail("MTP sequential: incompatible model configuration");
  if (b < 0 || b >= main.cfg.vocab || IsSequentialStop(b, stop_tokens))
    return Status::Fail("MTP sequential: bonus must be a non-stop token");
  for (int32_t token : stop_tokens)
    if (token < 0 || token >= main.cfg.vocab)
      return Status::Fail("MTP sequential: invalid stop token");
  if (mtp.k_max < 4 || !mtp.d_ms_vlogits || !mtp.d_ms_vtrunk ||
      !mtp.d_ms_ext_ids)
    return Status::Fail("MTP sequential: reserve scratch for four rows");
  return Status();
}

// All post-submission exits keep ownership until completion, including a
// failed launch/copy. The device prefix may have changed on failure; clearing
// outputs is deliberately not a rollback or permission to resume the request.
Status FinishSequential(const Status& submitted, MtpSequentialResult* result,
                        cudaStream_t stream) {
  const cudaError_t error = cudaStreamSynchronize(stream);
  Status status = submitted;
  if (error != cudaSuccess) {
    status = Status::Fail(std::string("MTP sequential completion: ") +
                          cudaGetErrorString(error));
  }
  if (!status.ok()) {
    const int draft_calls = result->draft_forward_calls;
    const int target_calls = result->target_forward_calls;
    const int extend_calls = result->extend_forward_calls;
    *result = MtpSequentialResult{};
    result->draft_forward_calls = draft_calls;
    result->target_forward_calls = target_calls;
    result->extend_forward_calls = extend_calls;
  }
  return status;
}

}  // namespace

Status MtpSequentialVerify(
    const model::Model& main, const MtpModel& mtp,
    const model::ModelSequence& seq, int32_t b,
    std::span<const int32_t, 3> drafts, int output_remaining,
    std::span<const int32_t> stop_tokens, MtpSequentialResult* result,
    cudaStream_t stream) {
  if (!result) return Status::Fail("MTP sequential: null result");
  *result = MtpSequentialResult{};
  Status status = CheckSequentialInput(main, mtp, seq, b, output_remaining,
                                       stop_tokens);
  if (!status.ok()) return status;
  for (int32_t draft : drafts)
    if (draft < 0 || draft >= main.cfg.vocab)
      return Status::Fail("MTP sequential: invalid draft token");

  // Match the ordinary scheduler's oldest-first PLE context exactly, without
  // publishing any speculative cursor/history changes to the request owner.
  const int width = main.ple_emb ? main.ple_hash.ngram_size - 1 : 0;
  std::vector<int32_t> history(static_cast<size_t>(width),
                              static_cast<int32_t>(main.cfg.eos_token_id));
  for (int j = 0; j < width; ++j) {
    const int source = seq.position - (width - j);
    if (source >= 0) history[j] = seq.history[source];
  }
  const int slot = 0;
  const int vocab = main.cfg.vocab;
  const size_t hc_dim = static_cast<size_t>(mtp.hc_dim());
  int32_t token = b;
  for (int row = 0; row < 4; ++row) {
    const int position = seq.position + row;
    uint16_t* logits = mtp.d_ms_vlogits + static_cast<size_t>(row) * vocab;
    uint16_t* trunk = mtp.d_ms_vtrunk + static_cast<size_t>(row) * hc_dim;
    ++result->target_forward_calls;
    status = model::ModelDecodeBatchMulti(main, &token, &position, &slot,
                                          history.data(), 1, logits, stream,
                                          trunk);
    if (!status.ok()) return FinishSequential(status, result, stream);
    // Use the ordinary scheduler's reduction, not the MTP draft reduction.
    status = model::ArgmaxBf16Rows(logits, 1, vocab, mtp.d_ms_ext_ids, stream);
    if (!status.ok()) return FinishSequential(status, result, stream);
    int32_t prediction = -1;
    const cudaError_t copied = cudaMemcpyAsync(
        &prediction, mtp.d_ms_ext_ids, sizeof(prediction),
        cudaMemcpyDeviceToHost, stream);
    status = FinishSequential(
        copied == cudaSuccess
            ? Status()
            : Status::Fail("MTP sequential: target argmax readback"),
        result, stream);
    if (!status.ok()) return status;
    if (prediction < 0 || prediction >= vocab)
      return FinishSequential(
          Status::Fail("MTP sequential: invalid target prediction"), result,
          stream);

    result->accepted_tokens[row] = token;
    result->accepted_count = row + 1;
    result->next_b = prediction;
    if (IsSequentialStop(prediction, stop_tokens)) {
      result->terminal = true;
      return Status();
    }
    if (row == 3 || drafts[row] != prediction) return Status();
    // Only an accepted non-stop prediction becomes the next target input.
    for (int j = 0; j + 1 < width; ++j) history[j] = history[j + 1];
    if (width > 0) history.back() = token;
    token = drafts[row];
  }
  return Status();
}

Status MtpSpeculativeStepSequentialTarget(
    const model::Model& main, const MtpModel& mtp,
    const model::ModelSequence& seq, int32_t b, int32_t d0,
    const uint16_t* g_in, int output_remaining,
    std::span<const int32_t> stop_tokens, MtpSequentialResult* result,
    uint16_t* next_g, cudaStream_t stream) {
  if (!result) return Status::Fail("MTP sequential: null result");
  *result = MtpSequentialResult{};
  Status status = CheckSequentialInput(main, mtp, seq, b, output_remaining,
                                       stop_tokens);
  if (!status.ok()) return status;
  if (!g_in || !next_g || d0 < 0 || d0 >= main.cfg.vocab)
    return Status::Fail("MTP sequential: invalid draft seed/output");
  if (!mtp.d_ms_ids || !mtp.d_ms_sample || !mtp.d_ms_multi ||
      !mtp.d_ms_g_pool || !mtp.d_ms_drafts ||
      !mtp.d_ms_ext_seq || !mtp.d_ms_ext_logits || !mtp.d_ms_ext_multi ||
      !mtp.d_ms_ext_sample)
    return Status::Fail("MTP sequential: incomplete draft/extend scratch");

  const int vocab = mtp.cfg.vocab;
  const size_t hc_dim = static_cast<size_t>(mtp.hc_dim());
  const size_t trunk_bytes = hc_dim * sizeof(uint16_t);
  const std::array<int, 4> slots{};
  const std::array<int, 2> draft_positions{seq.position, seq.position + 1};
  std::array<int32_t, 3> drafts{};
  if (cudaMemcpyAsync(mtp.d_ms_g_pool, g_in, trunk_bytes,
                      cudaMemcpyDeviceToDevice, stream) != cudaSuccess ||
      cudaMemcpyAsync(mtp.d_ms_drafts, &d0, sizeof(d0),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess ||
      cudaMemcpyAsync(mtp.d_ms_ext_seq, slots.data(), sizeof(slots),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess)
    return FinishSequential(Status::Fail("MTP sequential: draft seed copy"),
                            result, stream);

  // Same S1 draft arithmetic as the fast multi path: d_seq_id is non-null,
  // d0 is the previous extend's prediction, and two real forwards yield d1/d2.
  for (int j = 1; j < 3; ++j) {
    if (cudaMemcpyAsync(mtp.d_ms_ids, mtp.d_ms_drafts + j - 1,
                        sizeof(int32_t), cudaMemcpyDeviceToDevice, stream) !=
        cudaSuccess)
      return FinishSequential(Status::Fail("MTP sequential: draft input copy"),
                              result, stream);
    ++result->draft_forward_calls;
    status = MtpForward(mtp, mtp.d_ms_ids, &draft_positions[j - 1],
                        mtp.d_ms_g_pool, mtp.d_ms_sample, mtp.d_ms_multi,
                        mtp.d_ms_vlogits, 1, stream, mtp.d_ms_ext_seq, true,
                        model::LogitsRows::kAllRows, nullptr,
                        draft_positions[j - 1]);
    if (!status.ok()) return FinishSequential(status, result, stream);
    status = ArgmaxBf16Rows(mtp.d_ms_vlogits, 1, vocab,
                            mtp.d_ms_drafts + j, stream);
    if (!status.ok()) return FinishSequential(status, result, stream);
    if (cudaMemcpyAsync(mtp.d_ms_g_pool, mtp.d_ms_multi, trunk_bytes,
                        cudaMemcpyDeviceToDevice, stream) != cudaSuccess)
      return FinishSequential(Status::Fail("MTP sequential: draft trunk copy"),
                              result, stream);
  }
  const cudaError_t copied = cudaMemcpyAsync(
      drafts.data(), mtp.d_ms_drafts, sizeof(drafts), cudaMemcpyDeviceToHost,
      stream);
  status = FinishSequential(
      copied == cudaSuccess
          ? Status()
          : Status::Fail("MTP sequential: draft tokens readback"),
      result, stream);
  if (!status.ok()) return status;

  MtpSequentialResult verified;
  status = MtpSequentialVerify(main, mtp, seq, b, drafts, output_remaining,
                               stop_tokens, &verified, stream);
  verified.draft_forward_calls = result->draft_forward_calls;
  *result = verified;
  if (!status.ok()) return FinishSequential(status, result, stream);
  if (result->terminal) return Status();

  // EAGLE shift: target consumed [b, accepted drafts]; extend inputs are
  // [accepted drafts, correction] at those same positions, paired with the
  // actual sequential target trunks. Rows are contiguous: no gather needed.
  const int rows = result->accepted_count;
  std::array<int32_t, 4> extend_ids{};
  std::array<int, 4> extend_positions{};
  for (int row = 0; row < rows; ++row) {
    extend_ids[row] = row + 1 < rows ? result->accepted_tokens[row + 1]
                                    : result->next_b;
    extend_positions[row] = seq.position + row;
  }
  if (cudaMemcpyAsync(mtp.d_ms_ext_ids, extend_ids.data(),
                      static_cast<size_t>(rows) * sizeof(int32_t),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess)
    return FinishSequential(Status::Fail("MTP sequential: extend input copy"),
                            result, stream);
  ++result->extend_forward_calls;
  status = MtpForward(mtp, mtp.d_ms_ext_ids, extend_positions.data(),
                      mtp.d_ms_vtrunk, mtp.d_ms_ext_sample, mtp.d_ms_ext_multi,
                      mtp.d_ms_ext_logits, rows, stream, mtp.d_ms_ext_seq, true,
                      model::LogitsRows::kAllRows, nullptr,
                      extend_positions[rows - 1]);
  if (!status.ok()) return FinishSequential(status, result, stream);
  status = ArgmaxBf16Rows(
      mtp.d_ms_ext_logits + static_cast<size_t>(rows - 1) * vocab, 1, vocab,
      mtp.d_ms_ids, stream);
  if (!status.ok()) return FinishSequential(status, result, stream);
  int32_t next_d0 = -1;
  if (cudaMemcpyAsync(&next_d0, mtp.d_ms_ids, sizeof(next_d0),
                      cudaMemcpyDeviceToHost, stream) != cudaSuccess ||
      cudaMemcpyAsync(next_g,
                      mtp.d_ms_ext_multi + static_cast<size_t>(rows - 1) *
                                               hc_dim,
                      trunk_bytes, cudaMemcpyDeviceToDevice, stream) !=
          cudaSuccess)
    return FinishSequential(Status::Fail("MTP sequential: extend output copy"),
                            result, stream);
  status = FinishSequential(Status(), result, stream);
  if (!status.ok()) return status;
  if (next_d0 < 0 || next_d0 >= vocab)
    return FinishSequential(Status::Fail("MTP sequential: invalid next draft"),
                            result, stream);
  result->next_d0 = next_d0;
  result->next_seed_valid = true;
  return Status();
}

}  // namespace q4t::mtp
