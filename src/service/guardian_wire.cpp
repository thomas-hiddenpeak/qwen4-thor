#include "q4t/service/guardian_wire.h"

#include <charconv>
#include <cmath>
#include <limits>
#include <unordered_set>

namespace q4t::service::wire {
namespace {
void Escape(const std::string& value, std::string* output) {
  constexpr char kHex[] = "0123456789abcdef";
  output->push_back('"');
  for (unsigned char c : value) {
    if (c == '"' || c == '\\') {
      output->push_back('\\');
      output->push_back(static_cast<char>(c));
    } else if (c < 0x20) {
      output->append("\\u00");
      output->push_back(kHex[c >> 4]);
      output->push_back(kHex[c & 15]);
    } else {
      output->push_back(static_cast<char>(c));
    }
  }
  output->push_back('"');
}

bool Append(const io::Json& value, size_t depth, size_t* count,
            std::string* output) {
  if (depth > 24 || ++*count > 8192 || output->size() > kMaxFrame ||
      value.str.size() > kMaxFrame)
    return false;
  using Type = io::Json::Type;
  switch (value.type) {
    case Type::kNull:
      output->append("null");
      break;
    case Type::kBool:
      output->append(value.boolean ? "true" : "false");
      break;
    case Type::kNumber: {
      // Parsed private messages preserve the original spelling. Locally
      // constructed numbers are integral. Never round ledger nanoseconds or
      // revision numbers through double and serialize them back incorrectly.
      if (value.str.empty() || !std::isfinite(value.number)) return false;
      io::Json parsed;
      io::JsonParseLimits limits;
      limits.max_depth = 1;
      limits.max_values = 1;
      limits.max_string_bytes = 0;
      if (!io::ParseJson(value.str, &parsed, true, limits).ok() ||
          !parsed.IsNumber())
        return false;
      output->append(value.str);
      break;
    }
    case Type::kString:
      Escape(value.str, output);
      break;
    case Type::kArray:
      output->push_back('[');
      for (size_t i = 0; i < value.array.size(); ++i) {
        if (i) output->push_back(',');
        if (!Append(value.array[i], depth + 1, count, output)) return false;
      }
      output->push_back(']');
      break;
    case Type::kObject: {
      output->push_back('{');
      std::unordered_set<std::string> keys;
      for (const auto& [key, member] : value.object) {
        if (++*count > 8192 || key.size() > kMaxFrame ||
            !keys.insert(key).second)
          return false;
        if (keys.size() > 1) output->push_back(',');
        Escape(key, output);
        output->push_back(':');
        if (!Append(member, depth + 1, count, output)) return false;
      }
      output->push_back('}');
      break;
    }
  }
  return output->size() <= kMaxFrame;
}
}  // namespace

io::Json String(std::string text) {
  io::Json result;
  result.type = io::Json::Type::kString;
  result.str = std::move(text);
  return result;
}
io::Json Unsigned(uint64_t number) {
  io::Json result;
  result.type = io::Json::Type::kNumber;
  result.number = static_cast<double>(number);
  result.str = std::to_string(number);
  return result;
}
io::Json Boolean(bool value) {
  io::Json result;
  result.type = io::Json::Type::kBool;
  result.boolean = value;
  return result;
}
io::Json Object(
    std::initializer_list<std::pair<std::string, io::Json>> members) {
  io::Json result;
  result.type = io::Json::Type::kObject;
  result.object = members;
  return result;
}
Status Parse(const std::string& text, io::Json* result) {
  if (text.size() > kMaxFrame) return Status::Fail("oversized Guardian frame");
  io::JsonParseLimits limits;
  limits.max_depth = 24;
  limits.max_values = 8192;
  limits.max_string_bytes = kMaxFrame;
  limits.reject_duplicate_keys = true;
  return io::ParseJson(text, result, true, limits);
}
Status Encode(const io::Json& value, std::string* result) {
  if (result == nullptr) return Status::Fail("missing wire output");
  result->clear();
  size_t count = 0;
  if (!Append(value, 0, &count, result)) {
    result->clear();
    return Status::Fail("invalid or oversized Guardian message");
  }
  return Status{};
}
Status GetUnsigned(const io::Json& object, const std::string& key,
                   uint64_t* result) {
  const auto* value = object.Find(key);
  if (result == nullptr || value == nullptr || !value->IsNumber() ||
      value->str.empty())
    return Status::Fail("missing exact integer");
  uint64_t parsed = 0;
  const auto [end, error] = std::from_chars(
      value->str.data(), value->str.data() + value->str.size(), parsed);
  if (error != std::errc() || end != value->str.data() + value->str.size() ||
      parsed > static_cast<uint64_t>(std::numeric_limits<int64_t>::max())) {
    return Status::Fail("invalid exact integer");
  }
  *result = parsed;
  return Status{};
}
bool HasKeys(const io::Json& object, std::initializer_list<std::string> keys) {
  if (!object.IsObject() || object.object.size() != keys.size()) return false;
  for (const auto& key : keys)
    if (!object.Has(key)) return false;
  return true;
}
}  // namespace q4t::service::wire
