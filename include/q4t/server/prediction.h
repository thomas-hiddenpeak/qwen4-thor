#pragma once

#include <cstdint>
#include <span>
#include <string>
#include <unordered_map>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/sampling.h"
#include "q4t/status.h"

namespace q4t::server {

struct PredictionOptions {
  bool enabled = false;
  std::string content;
};

Status ParsePredictionOptions(const io::Json& request,
                              const SamplingOptions& sampling,
                              PredictionOptions* options,
                              std::string* error_param = nullptr);

// Static token proposal, independent of the target distribution. Consume is
// called only for a target-selected token actually entering the completion;
// a successful device lookahead alone must not advance acceptance or usage.
class PredictionCursor {
 public:
  explicit PredictionCursor(std::span<const int32_t> tokens);
  bool Matches(int32_t token) const;
  void Consume(int32_t token, bool content);
  int32_t Next() const;
  uint64_t Accepted() const { return accepted_; }
  uint64_t Rejected() const { return tokens_.size() - accepted_; }
  uint64_t Completion(uint64_t generated) const {
    return generated + Rejected();
  }

 private:
  size_t Match(int32_t token) const;
  std::vector<int32_t> tokens_;
  std::unordered_map<int32_t, std::vector<size_t>> positions_;
  size_t cursor_ = 0;
  uint64_t accepted_ = 0;
};

}  // namespace q4t::server
