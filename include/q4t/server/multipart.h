// Incremental RFC 7578 form-data decoder. HTTP framing/authentication and
// durable staging/publication belong to callers, not to the MIME parser.
#pragma once

#include <cstdint>
#include <functional>
#include <string>
#include <string_view>

#include "q4t/status.h"

namespace q4t::server {

struct MultipartPart {
  std::string name;
  std::string filename;
  std::string content_type;
  bool has_filename = false;
};

struct MultipartCallbacks {
  std::function<Status(const MultipartPart&)> begin;
  // Binary bytes, valid only until the callback returns. No transcoding.
  std::function<Status(std::string_view)> data;
  std::function<Status()> end;
};

// Parse a quoted/unquoted boundary without accepting duplicate parameters,
// CR/LF, invalid boundary characters, or a trailing boundary space.
Status MultipartBoundary(std::string_view content_type, std::string* boundary);

class MultipartDecoder {
 public:
  MultipartDecoder(std::string boundary, uint64_t max_wire_bytes,
                   size_t max_parts, MultipartCallbacks callbacks);
  // Feed arbitrary HTTP-decoded fragments, including embedded NUL bytes.
  // A failed callback/parser is sticky and never receives subsequent data.
  Status Feed(std::string_view bytes);
  // Required even after seeing a closing delimiter. Only successful Finish
  // authorizes the caller to publish staged content; destruction never does.
  Status Finish();
  size_t Parts() const { return parts_; }
  size_t BufferedBytes() const { return buffer_.size(); }
  uint64_t WireBytes() const { return wire_bytes_; }

 private:
  enum class State { kPreamble, kHeaders, kData, kEpilogue };
  Status Process(bool final);
  Status Headers(std::string_view headers);
  Status Emit(size_t bytes);
  Status Fail(std::string message);

  std::string boundary_, marker_, buffer_;
  uint64_t max_wire_bytes_ = 0, wire_bytes_ = 0;
  size_t max_parts_ = 0, parts_ = 0, ignored_bytes_ = 0;
  MultipartCallbacks callbacks_;
  State state_ = State::kPreamble;
  Status status_;
  bool finished_ = false;
};

}  // namespace q4t::server
