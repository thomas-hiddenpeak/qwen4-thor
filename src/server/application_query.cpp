#include "q4t/server/application_query.h"

#include <algorithm>
#include <charconv>
#include <unordered_set>

#include "object_protocol.h"
#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Invalid(std::string message, std::string param) {
  auto result = Error(400, "invalid_request", std::move(message));
  result.value = Object({{"param", String(std::move(param))}});
  return result;
}
int HexDigit(char value) {
  if (value >= '0' && value <= '9') return value - '0';
  if (value >= 'a' && value <= 'f') return value - 'a' + 10;
  if (value >= 'A' && value <= 'F') return value - 'A' + 10;
  return -1;
}
bool Decode(std::string_view encoded, std::string* result) {
  result->clear();
  for (size_t i = 0; i < encoded.size(); ++i) {
    unsigned char byte = encoded[i];
    if (byte == '%') {
      if (i + 2 >= encoded.size()) return false;
      const auto high = HexDigit(encoded[i + 1]);
      const auto low = HexDigit(encoded[i + 2]);
      if (high < 0 || low < 0) return false;
      byte = high * 16 + low;
      i += 2;
    } else if (byte == '+') {
      byte = ' ';
    }
    if (byte < 0x20 || byte == 0x7f) return false;
    result->push_back(byte);
  }
  // URL decoding must not turn malformed byte sequences into a different key
  // or value during subsequent JSON serialization. Preserve valid UTF-8 bytes.
  return detail::NormalizeUtf8(*result) == *result;
}
}  // namespace

ObjectReply ParseApplicationQuery(std::string_view raw, bool allow_status_list) {
  auto value = Object();
  if (raw.size() > 8192) return Invalid("Query exceeds byte limit", "query");
  std::unordered_set<std::string> seen;
  std::string include_key, status_key;
  while (!raw.empty()) {
    const auto end = raw.find('&');
    const auto part = raw.substr(0, end);
    const auto equal = part.find('=');
    std::string key, text;
    if (equal == part.npos || !Decode(part.substr(0, equal), &key) ||
        !Decode(part.substr(equal + 1), &text))
      return Invalid("Invalid query encoding", "query");
    const bool status = allow_status_list && (key == "status" || key == "status[]");
    if (status || key == "include" || key == "include[]") {
      const auto field = status ? "status" : "include";
      auto& encoding = status ? status_key : include_key;
      if (!encoding.empty() && encoding != key)
        return Invalid("Mixed array encodings", field);
      encoding = key;
      if (!value.Has(field)) Set(&value, field, Array());
      auto& items = Field(value, field).array;
      if (status || !std::ranges::any_of(items, [&](const Json& item) {
            return item.str == text;
          })) {
        if (items.size() >= 8) return Invalid("Too many array values", field);
        items.push_back(String(text));
      }
    } else {
      if (!seen.insert(key).second) return Invalid("Repeated query field", key);
      if (key == "limit" || key == "starting_after") {
        int64_t integer = 0;
        const auto parsed =
            std::from_chars(text.data(), text.data() + text.size(), integer);
        if (text.empty() || parsed.ec != std::errc() ||
            parsed.ptr != text.data() + text.size() ||
            integer < -9007199254740991LL || integer > 9007199254740991LL)
          return Invalid(key + " requires an exact integer", key);
        Set(&value, key, Number(integer));
      } else if (key == "stream" || key == "include_obfuscation") {
        if (text != "true" && text != "false")
          return Invalid(key + " requires true or false", key);
        Set(&value, key, Boolean(text == "true"));
      } else {
        Set(&value, key, String(text));
      }
    }
    if (end == raw.npos) break;
    raw.remove_prefix(end + 1);
    if (raw.empty()) return Invalid("Empty query field", "query");
  }
  ObjectReply result;
  result.value = std::move(value);
  return result;
}
}  // namespace q4t::server
