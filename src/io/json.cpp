// Minimal JSON parser implementation.
#include "q4t/io/json.h"

#include <cctype>
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

  explicit Parser(const std::string& text)
      : p(text.data()), end(text.data() + text.size()),
        begin(text.data()) {}

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
    SkipWs();
    if (p >= end) {
      Fail("unexpected end of input");
      return false;
    }
    char c = Peek();
    if (c == '{') return ParseObject(out);
    if (c == '[') return ParseArray(out);
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
    while (true) {
      SkipWs();
      if (Peek() != '"') {
        Fail("expected object key string");
        return false;
      }
      Json key;
      if (!ParseString(&key.str, &key)) return false;
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
            // Handle surrogate pairs.
            if (cp >= 0xD800 && cp <= 0xDBFF && p + 1 < end &&
                p[0] == '\\' && p[1] == 'u') {
              p += 2;
              unsigned lo = 0;
              if (!ParseHex4(&lo)) return false;
              if (lo >= 0xDC00 && lo <= 0xDFFF) {
                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
              }
            }
            AppendUtf8(out, cp);
            break;
          }
          default:
            Fail("bad escape char");
            return false;
        }
      } else {
        out->push_back(static_cast<char>(c));
        ++p;
      }
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
    if (Peek() == '-') ++p;
    while (p < end && std::isdigit(static_cast<unsigned char>(*p))) ++p;
    if (p < end && *p == '.') {
      ++p;
      while (p < end && std::isdigit(static_cast<unsigned char>(*p))) ++p;
    }
    if (p < end && (*p == 'e' || *p == 'E')) {
      ++p;
      if (p < end && (*p == '+' || *p == '-')) ++p;
      while (p < end && std::isdigit(static_cast<unsigned char>(*p))) ++p;
    }
    if (start == p) {
      Fail("expected value");
      return false;
    }
    std::string num(start, p);
    out->type = Json::Type::kNumber;
    out->number = std::strtod(num.c_str(), nullptr);
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
  return (v && v->IsNumber()) ? static_cast<int64_t>(v->number) : def;
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
  return IsNumber() ? static_cast<int64_t>(number) : def;
}

double Json::AsDouble(double def) const {
  return IsNumber() ? number : def;
}

Status ParseJson(const std::string& text, Json* out) {
  Parser parser(text);
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
