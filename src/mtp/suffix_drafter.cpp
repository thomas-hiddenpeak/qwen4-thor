// Trigram hashing and backward matching adapted from Strata 296b59d2:
// https://github.com/Niko1221/Strata/blob/296b59d2/src/spec/suffix_drafter.cpp
//
// MIT License
// Copyright (c) 2026 Niko1221 and the Strata contributors
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to
// deal in the Software without restriction, including without limitation the
// rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
// sell copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
// FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
// IN THE SOFTWARE.
#include "q4t/mtp/suffix_drafter.h"

#include <algorithm>
#include <chrono>
#include <new>

namespace q4t::mtp {
namespace {

using Clock = std::chrono::steady_clock;

class Elapsed {
 public:
  explicit Elapsed(uint64_t* total) : total_(total), start_(Clock::now()) {}
  ~Elapsed() {
    *total_ += static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() -
                                                             start_)
            .count());
  }

 private:
  uint64_t* total_;
  Clock::time_point start_;
};

uint64_t Mix(uint64_t value) {
  value ^= value >> 33;
  value *= 0xff51afd7ed558ccdULL;
  value ^= value >> 33;
  value *= 0xc4ceb9fe1a85ec53ULL;
  return value ^ (value >> 33);
}

}  // namespace

const char* SuffixReasonName(SuffixReason reason) {
  switch (reason) {
    case SuffixReason::kReady:
      return "ready";
    case SuffixReason::kInactive:
      return "inactive";
    case SuffixReason::kNoMatch:
      return "no_match";
    case SuffixReason::kIncomplete:
      return "incomplete";
    case SuffixReason::kFirstTokenMismatch:
      return "first_token_mismatch";
    case SuffixReason::kInvalidToken:
      return "invalid_token";
    case SuffixReason::kCapacityExhausted:
      return "capacity_exhausted";
  }
  return "unknown";
}

size_t SuffixDrafter::BucketFor(int32_t a, int32_t b, int32_t c) {
  const uint64_t key = Mix(
      static_cast<uint64_t>(static_cast<uint32_t>(a)) * 0x9E3779B97F4A7C15ULL ^
      Mix(static_cast<uint32_t>(b) + 0x632BE59BD9B4E019ULL) ^
      (static_cast<uint64_t>(static_cast<uint32_t>(c)) << 1));
  return static_cast<size_t>(key) & (kBucketCount - 1);
}

Status SuffixDrafter::Init(std::span<const int32_t> committed, int vocab,
                           size_t capacity) {
  Reset();
  counters_ = {};
  Elapsed elapsed(&counters_.init_ns);
  if (vocab <= 0 || capacity == 0 || capacity > kMaxCapacity ||
      committed.size() > capacity) {
    return Status::Fail("suffix init: invalid vocabulary/history/capacity");
  }
  vocab_ = vocab;
  if (!std::all_of(committed.begin(), committed.end(),
                   [this](int32_t token) { return Valid(token); })) {
    ++counters_.invalid_tokens;
    disabled_reason_ = SuffixReason::kInvalidToken;
    return Status::Fail("suffix init: invalid global token");
  }
  // Exact-sized arrays, rather than vector geometric growth or a hash map,
  // make the payload limit independent of request length and collisions.
  try {
    auto buckets = std::make_unique<Bucket[]>(kBucketCount);
    auto history = std::make_unique_for_overwrite<int32_t[]>(capacity);
    buckets_ = std::move(buckets);
    history_ = std::move(history);
  } catch (const std::bad_alloc&) {
    return Status::Fail("suffix init: bounded allocation failed");
  }
  capacity_ = capacity;
  active_ = true;
  disabled_reason_ = SuffixReason::kReady;
  counters_.peak_memory_bytes = MemoryBytes();
  AppendUnchecked(committed);
  counters_.initial_tokens = committed.size();
  return Status();
}

void SuffixDrafter::AppendUnchecked(std::span<const int32_t> committed) {
  for (int32_t token : committed) {
    const size_t end = size_++;
    history_[end] = token;
    if (end < 2) continue;
    auto& ends =
        buckets_[BucketFor(history_[end - 2], history_[end - 1], token)].ends;
    for (size_t i = kCandidates - 1; i > 0; --i) ends[i] = ends[i - 1];
    ends[0] = static_cast<uint32_t>(end);
  }
}

Status SuffixDrafter::Append(std::span<const int32_t> committed) {
  Elapsed elapsed(&counters_.append_ns);
  ++counters_.append_calls;
  if (!active_) return Status();
  if (committed.size() > capacity_ - size_) {
    active_ = false;
    disabled_reason_ = SuffixReason::kCapacityExhausted;
    ++counters_.capacity_exhaustions;
    return Status();
  }
  if (!std::all_of(committed.begin(), committed.end(),
                   [this](int32_t token) { return Valid(token); })) {
    active_ = false;
    disabled_reason_ = SuffixReason::kInvalidToken;
    ++counters_.invalid_tokens;
    return Status::Fail("suffix append: invalid committed global token");
  }
  AppendUnchecked(committed);
  counters_.appended_tokens += committed.size();
  return Status();
}

SuffixProposal SuffixDrafter::ProposeAfter(int32_t pending, int32_t d0) {
  Elapsed elapsed(&counters_.query_ns);
  ++counters_.queries;
  SuffixProposal result;
  if (!active_) {
    result.reason = disabled_reason_;
    return result;
  }
  if (!Valid(pending) || !Valid(d0)) {
    ++counters_.invalid_tokens;
    result.reason = SuffixReason::kInvalidToken;
    return result;
  }
  result.reason = SuffixReason::kNoMatch;
  if (size_ < 3) {
    ++counters_.no_match;
    return result;
  }
  const auto view = [this, pending](size_t position) {
    return position == size_ ? pending : history_[position];
  };
  const auto& ends =
      buckets_[BucketFor(view(size_ - 2), view(size_ - 1), pending)].ends;
  for (uint32_t end : ends) {
    if (end == UINT32_MAX) break;
    ++result.candidates_examined;
    int length = 0;
    while (length < kMaxMatch && static_cast<size_t>(length) <= end) {
      ++result.compared_tokens;
      if (history_[end - length] != view(size_ - length)) break;
      ++length;
    }
    // Ends are newest first. Strict > retains that order for equal lengths.
    if (length > result.match_length) {
      result.match_length = length;
      result.match_end = end;
    }
  }
  counters_.candidates_examined += result.candidates_examined;
  counters_.compared_tokens += result.compared_tokens;
  if (result.match_length < kMinMatch) {
    ++counters_.no_match;
    return result;
  }
  counters_.matched_tokens += static_cast<uint64_t>(result.match_length);
  if (size_ - result.match_end < kDraftCount) {
    ++counters_.incomplete;
    result.reason = SuffixReason::kIncomplete;
    return result;
  }
  for (size_t i = 0; i < result.tokens.size(); ++i) {
    result.tokens[i] = view(result.match_end + i + 1);
  }
  if (result.tokens[0] != d0) {
    ++counters_.first_token_mismatch;
    result.reason = SuffixReason::kFirstTokenMismatch;
    return result;
  }
  result.ready = true;
  result.reason = SuffixReason::kReady;
  ++counters_.hits;
  return result;
}

void SuffixDrafter::Reset() {
  Elapsed elapsed(&counters_.release_ns);
  buckets_.reset();
  history_.reset();
  size_ = 0;
  capacity_ = 0;
  vocab_ = 0;
  active_ = false;
  disabled_reason_ = SuffixReason::kInactive;
}

}  // namespace q4t::mtp
