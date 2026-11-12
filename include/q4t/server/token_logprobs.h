#pragma once

#include <deque>
#include <functional>
#include <span>
#include <string>
#include <string_view>

#include "q4t/server/chat_response_parser.h"
#include "q4t/server/completion_response.h"
#include "q4t/server/http_response.h"
#include "q4t/server/sampling.h"

namespace q4t::server {

// Tracks actual selected tokens through UTF-8 repair, stop lookahead and
// response parsing. Hidden-only tokens never enter content/refusal logprobs. A
// token crossing a visible boundary retains its actual bytes/probability, once;
// inventing a probability for a sliced or re-tokenized fragment is incorrect.
class TokenLogprobs {
 public:
  using Decode = std::function<Status(int32_t, std::string*)>;
  static constexpr size_t kStorageLimit = 16 * 1024 * 1024;

  explicit TokenLogprobs(bool collect, bool completion = false,
                          size_t initial_characters = 0,
                          bool exact_selected = false)
      : collect_(collect), completion_(completion),
        exact_selected_(exact_selected),
        emitted_characters_(initial_characters) {}
  Status Push(const SampledToken& sample, std::string_view decoded_bytes,
              const Decode& decode);
  void Map(std::span<const detail::Utf8Span> spans);
  Status Emit(const ResponseDelta& delta, std::string* json);
  void DiscardBefore(size_t normalized_offset);
  std::string Json() const;
  const CompletionProbabilityRows& CompletionRows() const {
    return completion_rows_;
  }

 private:
  struct Entry {
    size_t raw_begin = 0;
    size_t raw_end = 0;
    size_t mapped_end = 0;
    size_t begin = 0;
    size_t end = 0;
    bool mapped = false;
    bool emitted = false;
    std::string json;
    CompletionProbabilityRow completion;
  };
  bool collect_;
  bool completion_;
  bool exact_selected_;
  size_t emitted_bytes_ = 0;
  size_t emitted_characters_ = 0;
  CompletionProbabilityRows completion_rows_;
  size_t raw_bytes_ = 0;
  size_t queued_bytes_ = 0;
  std::deque<Entry> pending_;
  std::string collected_;
  std::string collected_refusal_;
  bool refusal_seen_ = false;
};

}  // namespace q4t::server
