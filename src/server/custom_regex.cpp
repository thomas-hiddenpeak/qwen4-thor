#include "custom_regex.h"

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <stdexcept>

#include "q4t/server/custom_tools.h"
#include "q4t/server/http_response.h"

namespace q4t::server {
#ifdef Q4T_ENABLE_XGRAMMAR
extern "C" {
struct RegexBuffer {
  uint8_t* data;
  size_t size;
  uint32_t error;
};
RegexBuffer q4t_regex_ebnf(const uint8_t* data, size_t size);
void q4t_regex_free(RegexBuffer buffer);
}
#endif
namespace {
void Require(bool condition) {
  if (!condition) throw std::runtime_error("invalid custom Lark source");
}
bool Letter(char c) { return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z'); }
int Hex(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}
std::string Quoted(std::string_view value) {
  return '"' + detail::JsonEscape(std::string(value)) + '"';
}
std::string Literal(std::string_view source) {
  // Normalize Python/Lark quoted literals through the same Unicode string
  // decoder as streamed custom payloads. A single quote never acts as framing.
  std::string json = "\"";
  for (size_t i = 0; i < source.size(); ++i) {
    const char c = source[i];
    if (c != '\\') {
      json += detail::JsonEscape(std::string(1, c));
      continue;
    }
    Require(++i < source.size());
    const char escape = source[i];
    if (escape == '\'')
      json += '\'';
    else if (escape == 'a')
      json += "\\u0007";
    else if (escape == 'v')
      json += "\\u000b";
    else if (escape == 'x' || escape == 'U') {
      const size_t count = escape == 'x' ? 2 : 8;
      Require(source.size() - i - 1 >= count);
      uint32_t cp = 0;
      for (size_t j = 0; j < count; ++j) {
        const int digit = Hex(source[++i]);
        Require(digit >= 0);
        cp = (cp << 4) | static_cast<uint32_t>(digit);
      }
      Require(cp <= 0x10ffff && !(cp >= 0xd800 && cp <= 0xdfff));
      char encoded[16];
      if (cp <= 0xffff) {
        std::snprintf(encoded, sizeof(encoded), "\\u%04x", cp);
      } else {
        cp -= 0x10000;
        std::snprintf(encoded, sizeof(encoded), "\\u%04x\\u%04x",
                      0xd800 + (cp >> 10), 0xdc00 + (cp & 1023));
      }
      json += encoded;
    } else if (std::string_view("\"\\/bfnrtu").find(escape) !=
               std::string_view::npos) {
      json += '\\';
      json += escape;
    } else {
      json += "\\\\";
      json += escape;
    }
  }
  json += '"';
  CustomInputStream decoder;
  size_t consumed = 0;
  std::string value;
  Require(decoder.Append(json, &consumed, &value).ok() && decoder.Done() &&
          consumed == json.size());
  return value;
}
std::string RegexLiteral(std::string_view value) {
  std::string pattern;
  for (const char c : value) {
    if (std::string_view("\\.^$|?*+()[]{}# ").find(c) != std::string_view::npos)
      pattern += '\\';
    pattern += c;
  }
  return pattern;
}
}  // namespace

std::string CustomRegexEbnf(std::string_view pattern) {
#ifdef Q4T_ENABLE_XGRAMMAR
  const auto buffer = q4t_regex_ebnf(
      reinterpret_cast<const uint8_t*>(pattern.data()), pattern.size());
  struct Guard {
    RegexBuffer value;
    ~Guard() { q4t_regex_free(value); }
  } guard{buffer};
  if (buffer.error == 2)
    throw std::length_error("custom regex expansion exceeds bound");
  if (buffer.error != 0 || !buffer.data || buffer.size > 512 * 1024)
    throw std::runtime_error("invalid custom regex grammar");
  return {reinterpret_cast<const char*>(buffer.data), buffer.size};
#else
  (void)pattern;
  throw std::runtime_error("custom grammar backend is not built");
#endif
}

CustomLarkSource PrepareCustomLark(std::string_view source) {
  struct Piece {
    bool regex;
    std::string text;
  };
  std::vector<Piece> pieces;
  const auto plain = [&](std::string_view text) {
    if (pieces.empty() || pieces.back().regex) pieces.push_back({false, {}});
    pieces.back().text += text;
  };
  for (size_t i = 0; i < source.size();) {
    if (source[i] == '#' || source.substr(i, 2) == "//") {
      const auto end = source.find('\n', i);
      plain(source.substr(i, end == source.npos ? source.size() - i : end - i));
      i = end == source.npos ? source.size() : end;
      continue;
    }
    const char quote = source[i];
    if (quote != '"' && quote != '\'' && quote != '/') {
      plain(source.substr(i++, 1));
      continue;
    }
    const auto start = ++i;
    while (i < source.size() && source[i] != quote) {
      if (source[i] == '\\') ++i;
      ++i;
    }
    Require(i < source.size());
    const auto raw = source.substr(start, i - start);
    const auto flags_start = ++i;
    while (i < source.size() && Letter(source[i])) ++i;
    const auto flags = source.substr(flags_start, i - flags_start);
    if (quote != '/' && flags.empty()) {
      plain(Quoted(Literal(raw)));
      continue;
    }
    Require(quote == '/' || flags == "i");
    auto pattern = quote == '/' ? std::string(raw) : RegexLiteral(Literal(raw));
    if (!flags.empty())
      pattern = "(?" + std::string(flags) + ':' + pattern + ')';
    pieces.push_back({true, CustomRegexEbnf(pattern)});
  }
  // Choose after decoding literals: an escaped spelling of a placeholder must
  // remain user text. Regex expansions are never subjected to substitution.
  std::string prefix = "Q4T_REGEX_PLACEHOLDER_";
  while (std::any_of(pieces.begin(), pieces.end(), [&](const Piece& piece) {
    return !piece.regex && piece.text.find(prefix) != piece.text.npos;
  }))
    prefix += '_';
  CustomLarkSource result;
  for (auto& piece : pieces) {
    if (!piece.regex) {
      result.text += piece.text;
      continue;
    }
    const auto marker = prefix + std::to_string(result.regexes.size()) + "_END";
    result.text += Quoted(marker);
    result.regexes.emplace_back(marker, std::move(piece.text));
  }
  return result;
}
}  // namespace q4t::server
