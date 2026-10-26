// Local, bounded causal experiment. No HTTP API can select these modes.
#pragma once

#include <array>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>
#include <string_view>

#include "q4t/mtp/suffix_drafter.h"
#include "q4t/status.h"

namespace q4t::server {

struct SuffixCausalChoice {
  std::string label;
  int input_tokens = 0;
  bool query = false;
  bool apply = false;
};

class SuffixCausalPlan {
 public:
  static constexpr size_t kMaxRequests = 24;

  Status Parse(std::string_view text, bool startup_suffix) {
    if (enabled_ || text.size() > 4096)
      return Status::Fail("causal plan already loaded or too large");
    std::istringstream input{std::string(text)};
    std::string version, startup;
    if (!(input >> version >> startup) || version != "suffix-causal-v1" ||
        startup != (startup_suffix ? "S1" : "S0"))
      return Status::Fail("causal plan startup mode mismatch");
    std::array<SuffixCausalChoice, kMaxRequests> entries;
    size_t size = 0;
    while (true) {
      input >> std::ws;
      if (input.eof()) break;
      SuffixCausalChoice entry;
      if (!(input >> entry.input_tokens >> entry.label) ||
          entry.input_tokens < 1 || entry.input_tokens > 208896 ||
          size == kMaxRequests)
        return Status::Fail("invalid causal request entry");
      if (entry.label != "N1" && entry.label != "N2" &&
          entry.label != "Q" && entry.label != "R")
        return Status::Fail("unknown causal request mode");
      entry.query = entry.label == "Q" || entry.label == "R";
      entry.apply = entry.label == "R";
      if (entry.query && !startup_suffix)
        return Status::Fail("causal query requires startup classification");
      entries[size++] = std::move(entry);
    }
    if (!size) return Status::Fail("empty causal request plan");
    entries_ = std::move(entries);
    size_ = size;
    enabled_ = true;
    return Status();
  }

  Status Load(const char* path, bool startup_suffix) {
    if (!path || path[0] != '/')
      return Status::Fail("causal plan requires an absolute local path");
    std::ifstream input(path, std::ios::binary);
    if (!input) return Status::Fail("cannot open causal plan");
    std::array<char, 4097> text;
    input.read(text.data(), text.size());
    if (input.bad() || input.gcount() > 4096)
      return Status::Fail("cannot read bounded causal plan");
    return Parse({text.data(), static_cast<size_t>(input.gcount())},
                 startup_suffix);
  }

  // Called only after the caller records completed prefill. Exhaustion or a
  // mismatched request fails closed; there is no wraparound or mode fallback.
  Status AfterPrefill(int input_tokens, uint64_t prefill_done_ns,
                      SuffixCausalChoice* choice, size_t* ordinal) {
    if (!enabled_ || !prefill_done_ns || !choice || !ordinal)
      return Status::Fail("causal selection requires completed prefill");
    const size_t index = next_.fetch_add(1);
    if (index >= size_ || entries_[index].input_tokens != input_tokens)
      return Status::Fail("causal request order exhausted or mismatched");
    *choice = entries_[index];
    *ordinal = index;
    return Status();
  }

  bool Enabled() const { return enabled_; }
  size_t Size() const { return size_; }
  size_t Consumed() const { return next_.load(); }

 private:
  std::array<SuffixCausalChoice, kMaxRequests> entries_;
  std::atomic<size_t> next_{0};
  size_t size_ = 0;
  bool enabled_ = false;
};

struct SuffixCausalRequest {
  explicit SuffixCausalRequest(bool observe) : enabled(observe) {}
  static uint64_t Now() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
  }
  void FirstContent(bool wrote, bool nonempty) {
    if (enabled && wrote && nonempty && !first_content_ns)
      first_content_ns = Now();
  }
  ~SuffixCausalRequest() {
    if (!enabled || request.empty()) return;
    // One bounded line after response/cleanup, never per-token logging.
    std::fprintf(stderr,
                 "[suffix-causal] request=%s ordinal=%zu mode=%s "
                 "input=%d arrive_ns=%llu target_done_ns=%llu "
                 "prefill_done_ns=%llu selected_ns=%llu first_round_ns=%llu "
                 "first_content_ns=%llu generation_done_ns=%llu "
                 "verify=%llu neural=%llu restore=%llu extend=%llu "
                 "extend_rows=%llu tail=%llu ready=%llu queries=%llu "
                 "hits=%llu init_ns=%llu query_ns=%llu append_ns=%llu "
                 "release_ns=%llu peak_bytes=%zu\n",
                 request.c_str(), ordinal, choice.label.c_str(), input,
                 static_cast<unsigned long long>(arrive_ns),
                 static_cast<unsigned long long>(target_done_ns),
                 static_cast<unsigned long long>(prefill_done_ns),
                 static_cast<unsigned long long>(selected_ns),
                 static_cast<unsigned long long>(first_round_ns),
                 static_cast<unsigned long long>(first_content_ns),
                 static_cast<unsigned long long>(generation_done_ns),
                 static_cast<unsigned long long>(verify),
                 static_cast<unsigned long long>(neural),
                 static_cast<unsigned long long>(restore),
                 static_cast<unsigned long long>(extend),
                 static_cast<unsigned long long>(extend_rows),
                 static_cast<unsigned long long>(tail),
                 static_cast<unsigned long long>(ready),
                 static_cast<unsigned long long>(stats.queries),
                 static_cast<unsigned long long>(stats.hits),
                 static_cast<unsigned long long>(stats.init_ns),
                 static_cast<unsigned long long>(stats.query_ns),
                 static_cast<unsigned long long>(stats.append_ns),
                 static_cast<unsigned long long>(stats.release_ns),
                 stats.peak_memory_bytes);
  }
  bool enabled;
  std::string request;
  SuffixCausalChoice choice;
  size_t ordinal = 0;
  int input = 0;
  uint64_t arrive_ns = 0, target_done_ns = 0, prefill_done_ns = 0;
  uint64_t selected_ns = 0, first_round_ns = 0, first_content_ns = 0;
  uint64_t generation_done_ns = 0;
  // Counts inferred from successful B1/k3 calls and the unchanged terminal
  // contract, not a separate GPU activity ledger.
  uint64_t verify = 0, neural = 0, restore = 0, extend = 0;
  uint64_t extend_rows = 0, tail = 0, ready = 0;
  mtp::SuffixCounters stats;
};

}  // namespace q4t::server
