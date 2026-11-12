// Small, allocation-bounded HTTP field/chunk syntax helpers. No I/O or policy.
#pragma once

#include <charconv>
#include <string>
#include <string_view>
#include <vector>

namespace q4t::server::http {

inline bool IsToken(unsigned char c) {
  return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
         (c >= '0' && c <= '9') ||
         std::string_view("!#$%&'*+-.^_`|~").find(c) != std::string_view::npos;
}

inline std::string_view Trim(std::string_view s) {
  while (!s.empty() && (s.front() == ' ' || s.front() == '\t'))
    s.remove_prefix(1);
  while (!s.empty() && (s.back() == ' ' || s.back() == '\t'))
    s.remove_suffix(1);
  return s;
}

inline std::string Lower(std::string_view s) {
  std::string result(s);
  for (char& c : result)
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  return result;
}

inline bool Field(std::string_view line, std::string* name,
                  std::string_view* value) {
  const auto colon = line.find(':');
  if (colon == line.npos || !colon) return false;
  for (unsigned char c : line.substr(0, colon))
    if (!IsToken(c)) return false;
  *name = Lower(line.substr(0, colon));
  *value = Trim(line.substr(colon + 1));
  for (unsigned char c : *value)
    if ((c < 32 && c != '\t') || c == 127) return false;
  return true;
}

inline bool Tokens(std::string_view value, std::vector<std::string>* out) {
  for (;;) {
    const auto comma = value.find(',');
    const auto token = Trim(value.substr(0, comma));
    if (token.empty() || out->size() >= 128) return false;
    for (unsigned char c : token)
      if (!IsToken(c)) return false;
    out->push_back(Lower(token));
    if (comma == value.npos) return true;
    value.remove_prefix(comma + 1);
  }
}

// Fields affecting framing, admission, routing, representation interpretation
// or credentials cannot be late-bound trailers or Connection options.
inline bool Protected(std::string_view name) {
  for (const auto field :
       {"host", "content-length", "transfer-encoding", "trailer", "expect",
        "authorization", "proxy-authorization", "proxy-authenticate", "cookie",
        "set-cookie", "content-type", "content-encoding", "content-range",
        "range", "x-request-id", "origin", "forwarded"})
    if (name == field) return true;
  return name.starts_with("x-q4t-") || name.starts_with("x-forwarded-") ||
         name.starts_with("access-control-");
}

inline bool ChunkSize(std::string_view line, size_t* size) {
  size_t digits = 0;
  while (digits < line.size()) {
    const char c = line[digits];
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
          (c >= 'A' && c <= 'F')))
      break;
    ++digits;
  }
  if (!digits) return false;
  const auto parsed =
      std::from_chars(line.data(), line.data() + digits, *size, 16);
  if (parsed.ec != std::errc{}) return false;
  line.remove_prefix(digits);
  while (!line.empty()) {
    line = Trim(line);
    if (line.empty() || line.front() != ';') return false;
    line.remove_prefix(1);
    line = Trim(line);
    size_t end = 0;
    while (end < line.size() && IsToken(line[end])) ++end;
    if (!end) return false;
    line.remove_prefix(end);
    if (line.empty()) return true;
    line = Trim(line);
    if (line.empty()) return false;
    if (line.front() == ';') continue;
    if (line.front() != '=') return false;
    line.remove_prefix(1);
    line = Trim(line);
    if (line.empty()) return false;
    if (line.front() != '"') {
      end = 0;
      while (end < line.size() && IsToken(line[end])) ++end;
      if (!end) return false;
      line.remove_prefix(end);
    } else {
      line.remove_prefix(1);
      bool closed = false;
      while (!line.empty()) {
        unsigned char c = line.front();
        line.remove_prefix(1);
        if (c == '"') {
          closed = true;
          break;
        }
        if (c == '\\') {
          if (line.empty()) return false;
          c = line.front();
          line.remove_prefix(1);
        }
        if ((c < 32 && c != '\t') || c == 127) return false;
      }
      if (!closed) return false;
    }
  }
  return true;
}

}  // namespace q4t::server::http
