#pragma once

#include <array>
#include <chrono>
#include <map>
#include <mutex>
#include <optional>
#include <span>
#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/trace/sha256.h"

namespace q4t::server {

// Optional, bounded, process-local observations. No raw tokens, prompts, tool
// definitions or cache keys are retained. Failure never enters the generation
// or durable-response transaction. Records intentionally expire on restart.
class PromptCacheDiagnostics {
 public:
  static constexpr size_t kMaxEntries = 128;
  static constexpr size_t kClientEntries = 16;
  static constexpr int kBlockTokens = 128;
  static constexpr int kMaxTokens = 262144;
  using Clock = std::chrono::steady_clock;
  struct Observation {
    std::array<trace::Digest, 7> settings;
    std::vector<trace::Digest> blocks;
    int reusable = 0;
    bool compacted = false;
  };
  static std::optional<Observation> Observe(const io::Json& settings,
                                            std::span<const int32_t> tokens,
                                            int reusable,
                                            bool compacted) noexcept;
  io::Json Compare(std::string_view scope, std::string_view comparison,
                   const io::Json& settings, std::span<const int32_t> tokens,
                   int restored, bool available, bool compacted,
                   Clock::time_point now = Clock::now()) noexcept;
  bool Remember(std::string_view scope, std::string_view id,
                Observation observation,
                Clock::time_point now = Clock::now()) noexcept;

 private:
  struct Record : Observation {
    std::string scope;
    Clock::time_point created;
  };
  static bool Settings(const io::Json& options,
                       std::array<trace::Digest, 7>* hashes);
  void Expire(Clock::time_point now);
  std::mutex mutex_;
  std::map<std::string, Record> records_;
};
}  // namespace q4t::server
