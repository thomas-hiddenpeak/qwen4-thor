#pragma once

#include <cstdint>
#include <string>
#include <string_view>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

struct CustomToolFormat {
  enum class Kind { kText, kRegex, kLark };
  Kind kind = Kind::kText;
  std::string definition;
};

bool SafeToolName(std::string_view name);
const io::Json* ToolDefinition(const io::Json& tool);
Status ParseCustomToolFormat(const io::Json& custom, CustomToolFormat* format,
                             std::string* error_param = nullptr,
                             std::string_view path = "custom");

// Model-only JSON string representation. Escape '<' so payload bytes cannot
// be confused with the surrounding native tool/control delimiters.
std::string EncodeCustomInput(std::string_view input);

// Incremental JSON string decoding. Append stops after the closing quote and
// reports consumed bytes; callers handle the outer model tag independently.
class CustomInputStream {
 public:
  Status Append(std::string_view text, size_t* consumed, std::string* decoded);
  bool Done() const { return state_ == State::kDone; }
  bool Started() const { return state_ != State::kStart; }

 private:
  enum class State {
    kStart,
    kText,
    kEscape,
    kUnicode,
    kLowSlash,
    kLowU,
    kDone
  };
  State state_ = State::kStart;
  uint32_t unicode_ = 0;
  uint32_t high_surrogate_ = 0;
  int digits_ = 0;
  std::string utf8_bytes_;
  uint32_t utf8_code_ = 0;
  uint32_t utf8_min_ = 0;
  int utf8_remaining_ = 0;
  bool failed_ = false;
};

}  // namespace q4t::server
