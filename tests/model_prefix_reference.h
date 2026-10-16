// Independent accepted-prefix execution for checkpoint boundary contracts.
// No speculative step or checkpoint save/restore participates in this oracle.
#pragma once

#include <algorithm>
#include <span>
#include <vector>

#include "q4t/model/model.h"

namespace q4t::test {

inline Status PrefixBoundaryReference(const model::Model& m,
                                      std::span<const int32_t> prompt,
                                      std::span<const int32_t> accepted,
                                      int32_t next, int seq_id,
                                      std::vector<uint16_t>* output) {
  const size_t rows = std::max(prompt.size(), accepted.size());
  if (prompt.empty() || accepted.empty() ||
      rows > static_cast<size_t>(m.cfg.max_prefill)) {
    return Status::Fail("prefix reference: invalid lengths");
  }
  uint16_t* logits = nullptr;
  if (cudaMalloc(&logits, rows * m.cfg.vocab * sizeof(uint16_t)) !=
      cudaSuccess) {
    return Status::Fail("prefix reference: allocation failed");
  }
  model::ModelSequence seq;
  Status s = model::ModelBeginSequence(m, &seq, nullptr, seq_id);
  if (s.ok()) {
    s = model::ModelPrefill(m, &seq, prompt.data(), prompt.size(), logits,
                            nullptr, nullptr, nullptr, seq_id);
  }
  if (s.ok()) {
    s = model::ModelDecodeBatch(m, accepted.data(), accepted.size(),
                                prompt.size(), prompt.data(), prompt.size(),
                                logits, nullptr, nullptr, false, seq_id);
    // This oracle owns logits and the accepted prefix through completion,
    // including the error path. Production checkpoint logic is not used.
    if (cudaStreamSynchronize(nullptr) != cudaSuccess) {
      s = Status::Fail("prefix reference: completion failed");
    }
  }
  if (s.ok()) {
    seq.history.assign(prompt.begin(), prompt.end());
    seq.history.insert(seq.history.end(), accepted.begin(), accepted.end());
    seq.position = seq.history.size();
    s = model::ModelDecodeStepSeq(m, &seq, next, logits, nullptr, nullptr,
                                  seq_id);
  }
  if (s.ok()) {
    output->resize(m.cfg.vocab);
    if (cudaMemcpy(output->data(), logits, output->size() * sizeof(uint16_t),
                   cudaMemcpyDeviceToHost) != cudaSuccess) {
      s = Status::Fail("prefix reference: readback failed");
    }
  }
  model::ModelEndSequence(&seq);
  cudaFree(logits);
  return s;
}

}  // namespace q4t::test
