// Bounded text stopping before response parsing/publication. No model access.
#pragma once

#include <string>
#include <string_view>
#include <vector>
#include <utility>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

inline constexpr std::string_view kTextStopProfile = "q4t-text-stop-v1";
inline constexpr size_t kMaxStopStrings = 4;
inline constexpr size_t kMaxStopBytes = 256;

// Missing/null/[] disable stopping. Validates shape and supported combinations
// before registration/GPU work; constraints_active is the shared output plan.
Status ParseTextStop(const io::Json& request, bool constraints_active,
                     std::vector<std::string>* stops,
                     std::string* error_param = nullptr);

class TextStopStream {
 public:
  explicit TextStopStream(std::vector<std::string> stops)
      : stops_(std::move(stops)) {}

  // Input must have passed Utf8Stream. Excludes the match and all later bytes.
  // Earliest completed match wins, ties use request list order. Keeps only a
  // suffix that could still become a stop string (at most max length - 1).
  std::string Append(std::string_view text);
  std::string Finish();  // Flush an unmatched suffix at EOS/length.
  bool Enabled() const { return !stops_.empty(); }
  bool Matched() const { return matched_index_ >= 0; }
  int MatchedIndex() const { return matched_index_; }
  size_t BufferedBytes() const { return pending_.size(); }

 private:
  std::vector<std::string> stops_;
  std::string pending_;
  int matched_index_ = -1;
  bool finished_ = false;
};

}  // namespace q4t::server
