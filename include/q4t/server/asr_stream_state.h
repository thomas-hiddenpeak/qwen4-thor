// Bounded sample scheduling and immutable prefixes for actual streaming ASR.
#pragma once

#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <vector>

#include "q4t/status.h"

namespace q4t::server {
struct AsrStreamStep {
  uint64_t epoch = 0, sequence = 0, samples = 0;
  bool final = false;
  bool operator==(const AsrStreamStep&) const = default;
};

// The executor sees cumulative real 16k samples, not a padded future window.
// The owner retains those samples until the matching step has stopped using
// them. Cancel seals scheduling but never frees a borrower's audio storage.
class AsrStreamSchedule {
 public:
  AsrStreamSchedule(uint64_t epoch, uint64_t chunk_samples,
                    uint64_t maximum_samples, uint64_t maximum_steps);
  bool Valid() const { return valid_; }
  Status Append(uint64_t epoch, uint64_t samples);
  Status Seal(uint64_t epoch);
  bool Ready() const;
  Status Next(uint64_t epoch, AsrStreamStep* step);
  Status Complete(const AsrStreamStep& step);
  Status Cancel(uint64_t epoch);
  bool Drained() const;
  bool Cancelled() const { return cancelled_; }
  bool InFlight() const { return active_.sequence != 0; }
  uint64_t Received() const { return received_; }
  uint64_t Evaluated() const { return evaluated_; }
  uint64_t Steps() const { return sequence_; }

 private:
  const uint64_t epoch_, chunk_, maximum_, maximum_steps_;
  bool valid_ = false, sealed_ = false, cancelled_ = false;
  uint64_t received_ = 0, evaluated_ = 0, sequence_ = 0;
  AsrStreamStep active_;
};

struct AsrStreamToken {
  int32_t id = -1;
  // Actual tokenizer bytes; no replacement, re-tokenization or invented text.
  std::string bytes;
  bool operator==(const AsrStreamToken&) const = default;
};
struct AsrStreamPrefixLimits {
  size_t tokens = 4096;
  size_t bytes = 64 * 1024;
  size_t unfixed_passes = 2;
  size_t rollback_tokens = 5;
  int32_t vocabulary = 151936;
};

// Serial owner. Each proposal must extend the exact prefix used by the real
// next model invocation. Only the remaining suffix may change. The caller
// keeps the original selected-token probabilities alongside these identities.
class AsrStreamPrefix {
 public:
  AsrStreamPrefix(uint64_t epoch, AsrStreamPrefixLimits limits = {});
  bool Valid() const { return valid_; }
  Status Accept(uint64_t epoch, std::span<const AsrStreamToken> hypothesis,
                bool actual_eos, bool final);
  Status Finalize(uint64_t epoch);
  Status Cancel(uint64_t epoch);
  std::span<const AsrStreamToken> Prefix() const {
    return std::span(latest_).first(fixed_);
  }
  std::span<const AsrStreamToken> Hypothesis() const { return latest_; }
  bool Finished() const { return finished_ && !cancelled_; }
  bool Failed() const { return failed_; }
  bool Cancelled() const { return cancelled_; }
  size_t Passes() const { return passes_; }

 private:
  Status Fail();
  const uint64_t epoch_;
  const AsrStreamPrefixLimits limits_;
  bool valid_ = false, failed_ = false, finished_ = false, cancelled_ = false;
  size_t fixed_ = 0, passes_ = 0;
  std::vector<AsrStreamToken> latest_;
};
}  // namespace q4t::server
