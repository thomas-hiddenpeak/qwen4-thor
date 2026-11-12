#include "q4t/server/custom_tools.h"

#include <algorithm>

#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
int Hex(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}
void Utf8(uint32_t code, std::string* output) {
  if (code <= 0x7f) {
    output->push_back(static_cast<char>(code));
  } else if (code <= 0x7ff) {
    output->push_back(static_cast<char>(0xc0 | (code >> 6)));
    output->push_back(static_cast<char>(0x80 | (code & 0x3f)));
  } else if (code <= 0xffff) {
    output->push_back(static_cast<char>(0xe0 | (code >> 12)));
    output->push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3f)));
    output->push_back(static_cast<char>(0x80 | (code & 0x3f)));
  } else {
    output->push_back(static_cast<char>(0xf0 | (code >> 18)));
    output->push_back(static_cast<char>(0x80 | ((code >> 12) & 0x3f)));
    output->push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3f)));
    output->push_back(static_cast<char>(0x80 | (code & 0x3f)));
  }
}
}  // namespace

bool SafeToolName(std::string_view name) {
  return !name.empty() && name.size() <= 64 &&
         std::all_of(name.begin(), name.end(), [](unsigned char c) {
           return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '_' || c == '-';
         });
}

const io::Json* ToolDefinition(const io::Json& tool) {
  return tool.Find(tool.GetString("type") == "custom" ? "custom" : "function");
}

Status ParseCustomToolFormat(const io::Json& custom, CustomToolFormat* format,
                             std::string* error_param, std::string_view path) {
  *format = {};
  const auto invalid = [&](std::string param, std::string message) {
    if (error_param) *error_param = std::move(param);
    return Status::Fail(std::move(message));
  };
  auto status =
      InputFields(custom, {"name", "description", "format"}, error_param, path);
  if (!status) return status;
  if (!SafeToolName(custom.GetString("name")))
    return invalid(std::string(path) + ".name", "invalid custom tool name");
  if (const auto* description = custom.Find("description");
      description && !description->IsString())
    return invalid(std::string(path) + ".description",
                   "expected description text");
  const auto* spec = custom.Find("format");
  if (!spec) return Status();
  const auto format_path = std::string(path) + ".format";
  const bool text = spec->GetString("type") == "text";
  status =
      text ? InputFields(*spec, {"type"}, error_param, format_path)
           : InputFields(*spec, {"type", "grammar"}, error_param, format_path);
  if (!status) return status;
  if (text) return Status();
  if (spec->GetString("type") != "grammar")
    return invalid(format_path + ".type", "expected text or grammar format");
  const auto* grammar = spec->Find("grammar");
  if (!grammar) return invalid(format_path + ".grammar", "missing grammar");
  status = InputFields(*grammar, {"syntax", "definition"}, error_param,
                       format_path + ".grammar");
  if (!status) return status;
  const auto syntax = grammar->GetString("syntax");
  if (syntax != "regex" && syntax != "lark")
    return invalid(format_path + ".grammar.syntax", "expected regex or lark");
  const auto* definition = grammar->Find("definition");
  if (!definition || !definition->IsString() || definition->str.empty() ||
      definition->str.size() > 16384)
    return invalid(format_path + ".grammar.definition",
                   "grammar definition must contain 1 to 16384 UTF-8 bytes");
  format->kind = syntax == "regex" ? CustomToolFormat::Kind::kRegex
                                   : CustomToolFormat::Kind::kLark;
  format->definition = definition->str;
  return Status();
}

std::string EncodeCustomInput(std::string_view input) {
  auto result = detail::JsonEscape(std::string(input));
  size_t at = 0;
  while ((at = result.find('<', at)) != std::string::npos) {
    result.replace(at, 1, "\\u003c");
    at += 6;
  }
  return '"' + result + '"';
}

Status CustomInputStream::Append(std::string_view text, size_t* consumed,
                                 std::string* decoded) {
  *consumed = 0;
  decoded->clear();
  if (failed_) return Status::Fail("custom input stream already failed");
  const auto invalid = [&]() {
    failed_ = true;
    return Status::Fail("invalid custom input JSON string");
  };
  for (unsigned char c : text) {
    if (Done()) break;
    ++*consumed;
    if (state_ == State::kText && utf8_remaining_) {
      if ((c & 0xc0) != 0x80) return invalid();
      utf8_code_ = (utf8_code_ << 6) | (c & 0x3f);
      utf8_bytes_.push_back(static_cast<char>(c));
      if (--utf8_remaining_ == 0) {
        if (utf8_code_ < utf8_min_ || utf8_code_ > 0x10ffff ||
            (utf8_code_ >= 0xd800 && utf8_code_ <= 0xdfff))
          return invalid();
        decoded->append(utf8_bytes_);
        utf8_bytes_.clear();
      }
      continue;
    }
    if (state_ == State::kText && c >= 0x80) {
      if (c >= 0xc2 && c <= 0xdf) {
        utf8_code_ = c & 0x1f;
        utf8_min_ = 0x80;
        utf8_remaining_ = 1;
      } else if (c >= 0xe0 && c <= 0xef) {
        utf8_code_ = c & 0x0f;
        utf8_min_ = 0x800;
        utf8_remaining_ = 2;
      } else if (c >= 0xf0 && c <= 0xf4) {
        utf8_code_ = c & 7;
        utf8_min_ = 0x10000;
        utf8_remaining_ = 3;
      } else {
        return invalid();
      }
      utf8_bytes_.push_back(static_cast<char>(c));
      continue;
    }
    switch (state_) {
      case State::kStart:
        if (c != '"') return invalid();
        state_ = State::kText;
        break;
      case State::kText:
        if (c == '"')
          state_ = State::kDone;
        else if (c == '\\')
          state_ = State::kEscape;
        else if (c < 0x20)
          return invalid();
        else
          decoded->push_back(static_cast<char>(c));
        break;
      case State::kLowSlash:
        if (c != '\\') return invalid();
        state_ = State::kLowU;
        break;
      case State::kLowU:
        if (c != 'u') return invalid();
        unicode_ = 0;
        digits_ = 0;
        state_ = State::kUnicode;
        break;
      case State::kEscape:
        state_ = State::kText;
        switch (c) {
          case '"':
          case '\\':
          case '/':
            decoded->push_back(c);
            break;
          case 'b':
            decoded->push_back('\b');
            break;
          case 'f':
            decoded->push_back('\f');
            break;
          case 'n':
            decoded->push_back('\n');
            break;
          case 'r':
            decoded->push_back('\r');
            break;
          case 't':
            decoded->push_back('\t');
            break;
          case 'u':
            unicode_ = 0;
            digits_ = 0;
            state_ = State::kUnicode;
            break;
          default:
            return invalid();
        }
        break;
      case State::kUnicode: {
        const int digit = Hex(static_cast<char>(c));
        if (digit < 0) return invalid();
        unicode_ = (unicode_ << 4) | static_cast<uint32_t>(digit);
        if (++digits_ < 4) break;
        if (high_surrogate_) {
          if (unicode_ < 0xdc00 || unicode_ > 0xdfff) return invalid();
          unicode_ = 0x10000 + ((high_surrogate_ - 0xd800) << 10) +
                     (unicode_ - 0xdc00);
          high_surrogate_ = 0;
        } else if (unicode_ >= 0xd800 && unicode_ <= 0xdbff) {
          high_surrogate_ = unicode_;
          state_ = State::kLowSlash;
          break;
        } else if (unicode_ >= 0xdc00 && unicode_ <= 0xdfff) {
          return invalid();
        }
        Utf8(unicode_, decoded);
        state_ = State::kText;
        break;
      }
      case State::kDone:
        break;
    }
  }
  return Status();
}

}  // namespace q4t::server
