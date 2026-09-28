// Host state of the existing runner. Included by model.h; no execution plan.
#pragma once

#include <algorithm>
#include <cstdint>
#include <span>
#include <vector>

#include "q4t/status.h"

namespace q4t::model {

enum class SequenceCompletion { kWait, kDeferred };

struct ModelSequence {
  enum class Stage { kIdle, kPrefill, kDecode, kFailed };
  Stage stage = Stage::kIdle;
  int position = 0;  // Committed consumed length; excludes the pending token.
  int seq_id = 0;
  std::vector<int32_t> history;  // Committed tokens only.

  bool HasPending() const { return pending_; }
  int SubmittedPosition() const {
    return position + static_cast<int>(submitted_.size());
  }

  // Caller resets the device state before publishing a fresh host state.
  Status Begin(int slot) {
    if (pending_ || slot < 0) return Status::Fail("sequence begin: busy/slot");
    seq_id = slot;
    position = 0;
    history.clear();
    stage = Stage::kPrefill;
    return Status();
  }

  // Reserve and copy before any device mutation. No committed cursor moves.
  Status Submit(std::span<const int32_t> tokens, Stage next, int max_len) {
    if (pending_ || (stage != Stage::kPrefill && stage != Stage::kDecode) ||
        (next != Stage::kPrefill && next != Stage::kDecode) ||
        (stage == Stage::kDecode && next != Stage::kDecode) || position < 0 ||
        position > max_len || tokens.empty() ||
        tokens.size() > static_cast<size_t>(max_len - position) ||
        history.size() != static_cast<size_t>(position)) {
      return Status::Fail("sequence submit: invalid state/range");
    }
    const size_t needed = history.size() + tokens.size();
    if (needed > history.capacity()) {
      history.reserve(std::min(static_cast<size_t>(max_len),
                               std::max(needed, history.capacity() * 2)));
    }
    submitted_.assign(tokens.begin(), tokens.end());
    next_stage_ = next;
    pending_ = true;
    return Status();
  }

  // Failure is visible immediately, but ownership lasts until completion.
  void Fail() { stage = Stage::kFailed; }

  Status End() {
    if (pending_) {
      Fail();
      return Status::Fail("sequence end: device work still pending");
    }
    stage = Stage::kIdle;
    position = 0;
    history.clear();
    return Status();
  }

 private:
  template <typename Wait>
  friend Status CompleteSequenceWork(std::span<ModelSequence* const>,
                                     const Status&, Wait&&);
  bool pending_ = false;
  Stage next_stage_ = Stage::kIdle;
  std::vector<int32_t> submitted_;
};

// One completion boundary for a whole batch. Always drain, including launch
// and copy failures. The production wait performs checked stream completion
// while the caller still holds exclusive ownership of model scratch.
template <typename Wait>
Status CompleteSequenceWork(std::span<ModelSequence* const> sequences,
                            const Status& submitted, Wait&& wait) {
  const Status completed = wait();
  Status result = completed.ok() ? submitted : completed;
  for (size_t i = 0; i < sequences.size(); ++i) {
    const auto* seq = sequences[i];
    if (!seq || !seq->pending_ || seq->stage == ModelSequence::Stage::kFailed) {
      if (result.ok())
        result = Status::Fail("sequence completion: no valid work");
    }
    for (size_t j = 0; j < i; ++j) {
      if (seq == sequences[j] && result.ok())
        result = Status::Fail("sequence completion: duplicate sequence");
    }
  }
  for (auto* seq : sequences) {
    if (!seq) continue;
    if (result.ok()) {
      seq->history.insert(seq->history.end(), seq->submitted_.begin(),
                          seq->submitted_.end());
      seq->position += static_cast<int>(seq->submitted_.size());
      seq->stage = seq->next_stage_;
    } else {
      seq->Fail();
    }
    seq->submitted_.clear();
    seq->pending_ = false;
  }
  return result;
}

}  // namespace q4t::model
