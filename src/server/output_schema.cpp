#include "q4t/server/output_schema.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <limits>
#include <unordered_set>
#include <vector>

#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
std::string Quote(std::string_view text) {
  return '"' + detail::JsonEscape(std::string(text)) + '"';
}

std::vector<std::string> Types(const io::Json& schema) {
  const auto* type = schema.Find("type");
  if (!type) return {};
  if (type->IsString()) return {type->str};
  std::vector<std::string> result;
  if (type->IsArray()) {
    for (const auto& item : type->array) {
      if (!item.IsString()) return {};
      result.push_back(item.str);
    }
  }
  return result;
}

bool Matches(const io::Json& value, const std::string& type) {
  if (type == "string") return value.IsString();
  if (type == "boolean") return value.IsBool();
  if (type == "null") return value.IsNull();
  if (type == "object") return value.IsObject();
  if (type == "array") return value.IsArray();
  if (type == "number") return value.IsNumber() && std::isfinite(value.number);
  if (type == "integer")
    return value.IsNumber() && IsExactJsonInteger(OutputJson(value));
  return false;
}

bool Has(const std::vector<std::string>& types, const char* type) {
  return std::find(types.begin(), types.end(), type) != types.end();
}

bool XmlName(const std::string& name) {
  return !name.empty() && name.size() <= 64 &&
         std::all_of(name.begin(), name.end(), [](unsigned char c) {
           return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '_' || c == '-';
         });
}

bool XmlMarker(const std::string& value) {
  for (const char* marker :
       {"<tool_call>", "</tool_call>", "</parameter>", "</function>"})
    if (value.find(marker) != value.npos) return true;
  return false;
}

Status Schema(const io::Json& schema, bool strict, bool xml, size_t depth,
              size_t* nodes) {
  if (!schema.IsObject() || depth > 8 || ++*nodes > 128)
    return Status::Fail("schema must be an object within depth 8 / 128 nodes");
  const auto types = Types(schema);
  if (types.empty() || types.size() > 2 ||
      (types.size() == 2 && (!Has(types, "null") || types[0] == types[1])))
    return Status::Fail("schema type must be a type or [type, null]");
  for (const auto& type : types) {
    if (type != "object" && type != "array" && type != "string" &&
        type != "integer" && type != "number" && type != "boolean" &&
        type != "null")
      return Status::Fail("unsupported schema type: " + type);
  }
  // A raw XML string cannot distinguish the string "null" from JSON null.
  if (xml && depth == 1 && Has(types, "string") && types.size() != 1)
    return Status::Fail("nullable top-level XML strings are not supported");
  for (const auto& [key, item] : schema.object) {
    if (key == "type") continue;
    if (key == "title" || key == "description") {
      if (!item.IsString() || item.str.size() > 2048)
        return Status::Fail("schema annotation must be a string <= 2048 bytes");
    } else if (key == "properties" || key == "required" ||
               key == "additionalProperties") {
      if (!Has(types, "object"))
        return Status::Fail("object keyword on a non-object schema");
    } else if (key == "items" || key == "minItems" || key == "maxItems") {
      if (!Has(types, "array"))
        return Status::Fail("array keyword on a non-array schema");
    } else if (key == "enum") {
      if (!item.IsArray() || item.array.empty() || item.array.size() > 32)
        return Status::Fail("enum must contain 1..32 scalar values");
      std::unordered_set<std::string> seen;
      for (const auto& value : item.array) {
        if (!(value.IsString() || value.IsBool() || value.IsNull()) ||
            (value.IsString() &&
             (value.str.size() > 256 || (xml && XmlMarker(value.str)))) ||
            !std::any_of(
                types.begin(), types.end(),
                [&](const auto& type) { return Matches(value, type); }) ||
            !seen.insert(OutputJson(value)).second)
          return Status::Fail(
              "enum requires distinct matching string/bool/null "
              "values without XML delimiters");
      }
    } else {
      return Status::Fail("unsupported schema keyword: " + key);
    }
  }
  if (Has(types, "object")) {
    const auto* properties = schema.Find("properties");
    const auto* additional = schema.Find("additionalProperties");
    if (!properties || !properties->IsObject() ||
        properties->object.size() > 32 || !additional ||
        !additional->IsBool() || additional->boolean)
      return Status::Fail(
          "object schema requires <=32 properties and "
          "additionalProperties=false");
    std::unordered_set<std::string> required;
    if (const auto* list = schema.Find("required")) {
      if (!list->IsArray()) return Status::Fail("required must be an array");
      for (const auto& key : list->array) {
        if (!key.IsString() || !properties->Has(key.str) ||
            !required.insert(key.str).second)
          return Status::Fail(
              "required keys must be unique declared properties");
      }
    }
    if (strict && required.size() != properties->object.size())
      return Status::Fail("strict schema requires every property in required");
    for (const auto& [key, child] : properties->object) {
      if (key.size() > 128 || (xml && depth == 0 && !XmlName(key)) ||
          (xml && XmlMarker(key)))
        return Status::Fail("invalid schema property name");
      auto status = Schema(child, strict, xml, depth + 1, nodes);
      if (!status) return status;
    }
  }
  if (Has(types, "array")) {
    const auto* items = schema.Find("items");
    if (!items) return Status::Fail("array schema requires items");
    for (const char* key : {"minItems", "maxItems"}) {
      const auto* bound = schema.Find(key);
      if (bound &&
          (!bound->IsNumber() || bound->AsInt(-1) < 0 || bound->AsInt(-1) > 64))
        return Status::Fail("array bounds must be integers in [0,64]");
    }
    if (schema.GetInt("minItems", 0) > schema.GetInt("maxItems", 64))
      return Status::Fail("minItems exceeds maxItems");
    auto status = Schema(*items, strict, xml, depth + 1, nodes);
    if (!status) return status;
  }
  return Status();
}

Status Value(const io::Json& schema, const io::Json& value, size_t depth) {
  if (depth > 32) return Status::Fail("output exceeds schema validation depth");
  const auto types = Types(schema);
  if (!std::any_of(types.begin(), types.end(),
                   [&](const auto& type) { return Matches(value, type); }))
    return Status::Fail("output type does not match schema");
  if (const auto* values = schema.GetArray("enum")) {
    if (!std::any_of(values->array.begin(), values->array.end(),
                     [&](const auto& item) {
                       return OutputJson(item) == OutputJson(value);
                     }))
      return Status::Fail("output is not a declared enum value");
  }
  if (value.IsObject()) {
    const auto* properties = schema.Find("properties");
    if (!properties) return Status::Fail("missing object properties");
    if (const auto* required = schema.GetArray("required")) {
      for (const auto& key : required->array)
        if (!value.Has(key.str))
          return Status::Fail("missing required property");
    }
    std::unordered_set<std::string> seen;
    for (const auto& [key, item] : value.object) {
      const auto* child = properties->Find(key);
      if (!child || !seen.insert(key).second)
        return Status::Fail("unknown or duplicate output property");
      auto status = Value(*child, item, depth + 1);
      if (!status) return status;
    }
  }
  if (value.IsArray()) {
    const auto* items = schema.Find("items");
    if (!items) return Status::Fail("missing array item schema");
    if (value.array.size() <
            static_cast<size_t>(schema.GetInt("minItems", 0)) ||
        (schema.Has("maxItems") &&
         value.array.size() > static_cast<size_t>(schema.GetInt("maxItems"))))
      return Status::Fail("output array length violates schema");
    for (const auto& item : value.array) {
      auto status = Value(*items, item, depth + 1);
      if (!status) return status;
    }
  }
  return Status();
}
}  // namespace

std::string OutputJson(const io::Json& value) {
  if (value.IsNull()) return "null";
  if (value.IsBool()) return value.boolean ? "true" : "false";
  if (value.IsString()) return Quote(value.str);
  if (value.IsNumber()) {
    if (!value.str.empty()) return value.str;
    char buffer[64];
    const auto result = std::to_chars(
        buffer, buffer + sizeof(buffer), value.number,
        std::chars_format::general, std::numeric_limits<double>::max_digits10);
    return result.ec == std::errc() ? std::string(buffer, result.ptr) : "null";
  }
  std::string out = value.IsArray() ? "[" : "{";
  if (value.IsArray()) {
    for (const auto& item : value.array) {
      if (out.size() != 1) out += ',';
      out += OutputJson(item);
    }
  } else {
    for (const auto& [key, item] : value.object) {
      if (out.size() != 1) out += ',';
      out += Quote(key) + ':' + OutputJson(item);
    }
  }
  return out + (value.IsArray() ? ']' : '}');
}

bool IsExactJsonInteger(std::string_view number) {
  const auto e = number.find_first_of("eE");
  const auto mantissa = number.substr(0, e);
  int exponent = 0;
  if (e != number.npos) {
    auto suffix = number.substr(e + 1);
    if (suffix.starts_with('+')) suffix.remove_prefix(1);
    const auto result =
        std::from_chars(suffix.data(), suffix.data() + suffix.size(), exponent);
    if (result.ec != std::errc() || result.ptr != suffix.data() + suffix.size())
      return false;
  }
  const auto dot = mantissa.find('.');
  const int64_t decimals =
      dot == mantissa.npos ? 0
                           : static_cast<int64_t>(mantissa.size() - dot - 1);
  int64_t needed = decimals - static_cast<int64_t>(exponent);
  if (needed <= 0) return true;
  for (auto i = mantissa.rbegin(); i != mantissa.rend(); ++i) {
    if (*i == '.' || *i == '-') continue;
    if (*i != '0') return false;
    if (--needed == 0) return true;
  }
  return true;
}

Status ValidateOutputSchema(const io::Json& schema, bool strict,
                            bool xml_parameters) {
  if (OutputJson(schema).size() > 16384)
    return Status::Fail("schema exceeds 16 KiB");
  if (schema.GetString("type") != "object")
    return Status::Fail("output/parameters schema root must be an object");
  size_t nodes = 0;
  return Schema(schema, strict, xml_parameters, 0, &nodes);
}

Status ValidateOutputValue(const io::Json& schema, const io::Json& value) {
  return Value(schema, value, 0);
}

}  // namespace q4t::server
