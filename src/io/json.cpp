// Minimal JSON parser implementation.
#include "q4t/io/json.h"

#include <cctype>
#include <charconv>
#include <cmath>
#include <unordered_set>
#include <cstdlib>
#include <cstring>

namespace q4t {
namespace io {

namespace {

struct Parser {
  const char* p;
  const char* end;
  const char* begin;
  std::string err;
  bool preserve_number_text;
  JsonParseLimits limits;
  size_t depth = 0;
  size_t values = 0;
  size_t string_bytes = 0;

  explicit Parser(const std::string& text, bool preserve_numbers,
                  const JsonParseLimits& parse_limits)
      : p(text.data()), end(text.data() + text.size()),
        begin(text.data()), preserve_number_text(preserve_numbers),
        limits(parse_limits) {}

  bool Ok() const { return err.empty(); }
  bool Fail(const std::string& msg) {
    if (err.empty()) {
      err = msg + " at offset " + std::to_string(p - begin);
    }
    return false;
  }

  void SkipWs() {
    while (p < end) {
      char c = *p;
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
        ++p;
      } else {
        break;
      }
    }
  }

  char Peek() const { return p < end ? *p : '\0'; }
  bool Consume(char c) {
    if (p < end && *p == c) {
      ++p;
      return true;
    }
    return false;
  }

  bool ParseValue(Json* out) {
    if (values >= limits.max_values) return Fail("JSON value limit");
    ++values;
    SkipWs();
    if (p >= end) {
      Fail("unexpected end of input");
      return false;
    }
    char c = Peek();
    if (c == '{' || c == '[') {
      if (depth >= limits.max_depth) return Fail("JSON depth limit");
      ++depth;
      const bool ok = c == '{' ? ParseObject(out) : ParseArray(out);
      --depth;
      return ok;
    }
    if (c == '"') return ParseString(&out->str, out);
    if (c == 't' || c == 'f') return ParseBool(out);
    if (c == 'n') return ParseNull(out);
    return ParseNumber(out);
  }

  bool ParseObject(Json* out) {
    out->type = Json::Type::kObject;
    if (!Consume('{')) return Fail("expected '{'");
    SkipWs();
    if (Consume('}')) return true;
    std::unordered_set<std::string> keys;
    while (true) {
      SkipWs();
      if (Peek() != '"') {
        Fail("expected object key string");
        return false;
      }
      if (values >= limits.max_values) return Fail("JSON value limit");
      ++values;
      Json key;
      if (!ParseString(&key.str, &key)) return false;
      if (limits.reject_duplicate_keys && !keys.insert(key.str).second)
        return Fail("duplicate object key");
      SkipWs();
      if (!Consume(':')) {
        Fail("expected ':' after key");
        return false;
      }
      Json val;
      if (!ParseValue(&val)) return false;
      out->object.emplace_back(std::move(key.str), std::move(val));
      SkipWs();
      if (Consume(',')) continue;
      if (Consume('}')) return true;
      Fail("expected ',' or '}' in object");
      return false;
    }
  }

  bool ParseArray(Json* out) {
    out->type = Json::Type::kArray;
    if (!Consume('[')) return Fail("expected '['");
    SkipWs();
    if (Consume(']')) return true;
    while (true) {
      Json val;
      if (!ParseValue(&val)) return false;
      out->array.push_back(std::move(val));
      SkipWs();
      if (Consume(',')) continue;
      if (Consume(']')) return true;
      Fail("expected ',' or ']' in array");
      return false;
    }
  }

  bool ParseString(std::string* out, Json* type_out) {
    if (!Consume('"')) {
      Fail("expected string");
      return false;
    }
    out->clear();
    while (p < end) {
      const size_t before = out->size();
      unsigned char c = static_cast<unsigned char>(*p);
      if (c == '"') {
        ++p;
        if (type_out) type_out->type = Json::Type::kString;
        return true;
      }
      if (c == '\\') {
        ++p;
        if (p >= end) {
          Fail("bad escape");
          return false;
        }
        char e = *p++;
        switch (e) {
          case '"': out->push_back('"'); break;
          case '\\': out->push_back('\\'); break;
          case '/': out->push_back('/'); break;
          case 'b': out->push_back('\b'); break;
          case 'f': out->push_back('\f'); break;
          case 'n': out->push_back('\n'); break;
          case 'r': out->push_back('\r'); break;
          case 't': out->push_back('\t'); break;
          case 'u': {
            unsigned cp = 0;
            if (!ParseHex4(&cp)) return false;
            if (cp >= 0xD800 && cp <= 0xDBFF) {
              if (end - p < 2 || p[0] != '\\' || p[1] != 'u')
                return Fail("missing low surrogate");
              p += 2;
              unsigned lo = 0;
              if (!ParseHex4(&lo)) return false;
              if (lo < 0xDC00 || lo > 0xDFFF)
                return Fail("invalid low surrogate");
              cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
            } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
              return Fail("unpaired low surrogate");
            }
            AppendUtf8(out, cp);
            break;
          }
          default:
            Fail("bad escape char");
            return false;
        }
      } else {
        if (c < 0x20) return Fail("unescaped string control character");
        if (c < 0x80) {
          out->push_back(static_cast<char>(c));
          ++p;
        } else {
          const int n = c >= 0xC2 && c <= 0xDF ? 2 :
                        c >= 0xE0 && c <= 0xEF ? 3 :
                        c >= 0xF0 && c <= 0xF4 ? 4 : 0;
          if (n == 0 || end - p < n) return Fail("invalid UTF-8");
          unsigned cp = c & ((1u << (7 - n)) - 1);
          for (int i = 1; i < n; ++i) {
            const auto next = static_cast<unsigned char>(p[i]);
            if ((next & 0xC0) != 0x80) return Fail("invalid UTF-8");
            cp = (cp << 6) | (next & 0x3F);
          }
          if ((n == 3 && cp < 0x800) || (n == 4 && cp < 0x10000) ||
              (cp >= 0xD800 && cp <= 0xDFFF) || cp > 0x10FFFF)
            return Fail("invalid UTF-8 scalar");
          out->append(p, n);
          p += n;
        }
      }
      const size_t added = out->size() - before;
      if (added > limits.max_string_bytes - string_bytes)
        return Fail("JSON string byte limit");
      string_bytes += added;
    }
    Fail("unterminated string");
    return false;
  }

  bool ParseHex4(unsigned* out) {
    if (end - p < 4) {
      Fail("bad \\u escape");
      return false;
    }
    unsigned v = 0;
    for (int i = 0; i < 4; ++i) {
      char c = *p++;
      v <<= 4;
      if (c >= '0' && c <= '9') v |= static_cast<unsigned>(c - '0');
      else if (c >= 'a' && c <= 'f') v |= static_cast<unsigned>(c - 'a' + 10);
      else if (c >= 'A' && c <= 'F') v |= static_cast<unsigned>(c - 'A' + 10);
      else {
        Fail("bad hex digit in \\u");
        return false;
      }
    }
    *out = v;
    return true;
  }

  void AppendUtf8(std::string* out, unsigned cp) {
    if (cp < 0x80) {
      out->push_back(static_cast<char>(cp));
    } else if (cp < 0x800) {
      out->push_back(static_cast<char>(0xC0 | (cp >> 6)));
      out->push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp < 0x10000) {
      out->push_back(static_cast<char>(0xE0 | (cp >> 12)));
      out->push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
      out->push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else {
      out->push_back(static_cast<char>(0xF0 | (cp >> 18)));
      out->push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
      out->push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
      out->push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    }
  }

  bool ParseBool(Json* out) {
    if (end - p >= 4 && std::memcmp(p, "true", 4) == 0) {
      p += 4;
      out->type = Json::Type::kBool;
      out->boolean = true;
      return true;
    }
    if (end - p >= 5 && std::memcmp(p, "false", 5) == 0) {
      p += 5;
      out->type = Json::Type::kBool;
      out->boolean = false;
      return true;
    }
    Fail("expected true/false");
    return false;
  }

  bool ParseNull(Json* out) {
    if (end - p >= 4 && std::memcmp(p, "null", 4) == 0) {
      p += 4;
      out->type = Json::Type::kNull;
      return true;
    }
    Fail("expected null");
    return false;
  }

  bool ParseNumber(Json* out) {
    const char* start = p;
    Consume('-');
    const auto digit = [&] { return p < end && *p >= '0' && *p <= '9'; };
    if (Consume('0')) {
      if (digit()) return Fail("leading zero in number");
    } else {
      if (!digit()) return Fail("expected number digit");
      while (digit()) ++p;
    }
    if (Consume('.')) {
      if (!digit()) return Fail("missing fractional digits");
      while (digit()) ++p;
    }
    if (Consume('e') || Consume('E')) {
      if (!Consume('+')) Consume('-');
      if (!digit()) return Fail("missing exponent digits");
      while (digit()) ++p;
    }
    double number = 0;
    const auto result = std::from_chars(start, p, number);
    if (result.ec != std::errc{} || result.ptr != p || !std::isfinite(number))
      return Fail("number outside supported finite range");
    out->type = Json::Type::kNumber;
    out->number = number;
    out->str = preserve_number_text ? std::string(start, p) : std::string();
    return true;
  }
};

}  // namespace

const Json* Json::Find(const std::string& key) const {
  if (type != Type::kObject) return nullptr;
  for (const auto& kv : object) {
    if (kv.first == key) return &kv.second;
  }
  return nullptr;
}

double Json::GetNumber(const std::string& key, double def) const {
  const Json* v = Find(key);
  return (v && v->IsNumber()) ? v->number : def;
}

int64_t Json::GetInt(const std::string& key, int64_t def) const {
  const Json* v = Find(key);
  return v ? v->AsInt(def) : def;
}

std::string Json::GetString(const std::string& key,
                            const std::string& def) const {
  const Json* v = Find(key);
  return (v && v->IsString()) ? v->str : def;
}

bool Json::GetBool(const std::string& key, bool def) const {
  const Json* v = Find(key);
  return (v && v->IsBool()) ? v->boolean : def;
}

const Json* Json::GetArray(const std::string& key) const {
  const Json* v = Find(key);
  return (v && v->IsArray()) ? v : nullptr;
}

int64_t Json::AsInt(int64_t def) const {
  // 2^63 is exactly representable as double; INT64_MAX is not.
  if (!IsNumber() || !std::isfinite(number) || number < -0x1p63 ||
      number >= 0x1p63 || std::trunc(number) != number) return def;
  return static_cast<int64_t>(number);
}

double Json::AsDouble(double def) const {
  return IsNumber() ? number : def;
}

Status ParseJson(const std::string& text, Json* out,
                 bool preserve_number_text, const JsonParseLimits& limits) {
  *out = Json{};
  Parser parser(text, preserve_number_text, limits);
  if (!parser.ParseValue(out)) {
    return Status::Fail("JSON parse error: " + parser.err);
  }
  parser.SkipWs();
  if (parser.p != parser.end) {
    return Status::Fail("JSON parse error: trailing characters at offset " +
                        std::to_string(parser.p - parser.begin));
  }
  return Status();
}

}  // namespace io
}  // namespace q4t
