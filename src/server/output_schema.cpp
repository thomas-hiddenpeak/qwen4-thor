#include "q4t/server/output_schema.h"
#include "q4t/server/input_contract.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <limits>
#include <unordered_set>
#include <vector>

#include "q4t/server/http_response.h"
#include "q4t/server/schema_graph.h"

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
              size_t* nodes, const std::string& path,
              std::string* error_param) {
  const auto invalid = [&](const std::string& location, std::string message) {
    if (error_param) *error_param = location;
    return Status::Fail(std::move(message));
  };
  if (!schema.IsObject() || depth > 8 || ++*nodes > 128)
    return invalid(path, "schema must be an object within depth 8 / 128 nodes");
  const auto types = Types(schema);
  if (types.empty() || types.size() > 2 ||
      (types.size() == 2 && (!Has(types, "null") || types[0] == types[1])))
    return invalid(path + ".type",
                   "schema type must be a type or [type, null]");
  for (size_t index = 0; index < types.size(); ++index) {
    const auto& type = types[index];
    if (type != "object" && type != "array" && type != "string" &&
        type != "integer" && type != "number" && type != "boolean" &&
        type != "null")
      return invalid(path + ".type" +
                         (schema.Find("type")->IsArray()
                              ? "[" + std::to_string(index) + "]"
                              : ""),
                     "unsupported schema type: " + type);
  }
  // A raw XML string cannot distinguish the string "null" from JSON null.
  if (xml && depth == 1 && Has(types, "string") && types.size() != 1)
    return invalid(path + ".type",
                   "nullable top-level XML strings are not supported");
  for (const auto& [key, item] : schema.object) {
    const auto location = InputParamPath(path, key);
    if (key == "type") continue;
    if (key == "title" || key == "description") {
      if (!item.IsString() || item.str.size() > 2048)
        return invalid(location,
                       "schema annotation must be a string <= 2048 bytes");
    } else if (key == "properties" || key == "required" ||
               key == "additionalProperties") {
      if (!Has(types, "object"))
        return invalid(location, "object keyword on a non-object schema");
    } else if (key == "items" || key == "minItems" || key == "maxItems") {
      if (!Has(types, "array"))
        return invalid(location, "array keyword on a non-array schema");
    } else if (key == "enum") {
      if (!item.IsArray() || item.array.empty() || item.array.size() > 32)
        return invalid(location, "enum must contain 1..32 scalar values");
      std::unordered_set<std::string> seen;
      for (size_t index = 0; index < item.array.size(); ++index) {
        const auto& value = item.array[index];
        if (!(value.IsString() || value.IsBool() || value.IsNull()) ||
            (value.IsString() &&
             (value.str.size() > 256 || (xml && XmlMarker(value.str)))) ||
            !std::any_of(
                types.begin(), types.end(),
                [&](const auto& type) { return Matches(value, type); }) ||
            !seen.insert(OutputJson(value)).second)
          return invalid(location + "[" + std::to_string(index) + "]",
                         "enum requires distinct matching string/bool/null "
                         "values without XML delimiters");
      }
    } else {
      return invalid(location, "unsupported schema keyword: " + key);
    }
  }
  if (Has(types, "object")) {
    const auto* properties = schema.Find("properties");
    const auto* additional = schema.Find("additionalProperties");
    const bool invalid_properties = !properties || !properties->IsObject() ||
                                    properties->object.size() > 32;
    if (invalid_properties || !additional || !additional->IsBool() ||
        additional->boolean)
      return invalid(
          path + (invalid_properties ? ".properties" : ".additionalProperties"),
          "object schema requires <=32 properties and "
          "additionalProperties=false");
    std::unordered_set<std::string> required;
    if (const auto* list = schema.Find("required")) {
      if (!list->IsArray())
        return invalid(path + ".required", "required must be an array");
      for (size_t index = 0; index < list->array.size(); ++index) {
        const auto& key = list->array[index];
        if (!key.IsString() || !properties->Has(key.str) ||
            !required.insert(key.str).second)
          return invalid(path + ".required[" + std::to_string(index) + "]",
                         "required keys must be unique declared properties");
      }
    }
    if (strict && required.size() != properties->object.size())
      return invalid(path + ".required",
                     "strict schema requires every property in required");
    for (const auto& [key, child] : properties->object) {
      const auto child_path = InputParamPath(path + ".properties", key);
      if (key.size() > 128 || (xml && depth == 0 && !XmlName(key)) ||
          (xml && XmlMarker(key)))
        return invalid(child_path, "invalid schema property name");
      auto status =
          Schema(child, strict, xml, depth + 1, nodes, child_path, error_param);
      if (!status) return status;
    }
  }
  if (Has(types, "array")) {
    const auto* items = schema.Find("items");
    if (!items) return invalid(path + ".items", "array schema requires items");
    for (const char* key : {"minItems", "maxItems"}) {
      const auto* bound = schema.Find(key);
      if (bound &&
          (!IsInputInteger(*bound) || bound->number < 0 || bound->number > 64))
        return invalid(InputParamPath(path, key),
                       "array bounds must be integers in [0,64]");
    }
    if (schema.GetInt("minItems", 0) > schema.GetInt("maxItems", 64))
      return invalid(path + ".minItems", "minItems exceeds maxItems");
    auto status = Schema(*items, strict, xml, depth + 1, nodes, path + ".items",
                         error_param);
    if (!status) return status;
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
                            bool xml_parameters, std::string* error_param,
                            std::string_view path) {
  (void)xml_parameters;  // Representation is selected after graph validation.
  OutputSchemaGraph graph;
  return graph.Init(schema, strict, error_param, path);
}

Status ValidateOutputValue(const io::Json& schema, const io::Json& value) {
  OutputSchemaGraph graph;
  auto status = graph.Init(schema, false, nullptr);
  return status ? graph.Check(value) : status;
}

bool NeedsSchemaGuard(const io::Json& schema) {
  size_t nodes = 0;
  return OutputJson(schema).size() > 16384 ||
         !Schema(schema, false, false, 0, &nodes, "schema", nullptr);
}

bool NeedsJsonToolArguments(const io::Json& function) {
  const auto* parameters = function.Find("parameters");
  if (!parameters) return false;
  if (parameters->GetString("type") != "object" ||
      OutputJson(*parameters).size() > 16384)
    return true;
  size_t nodes = 0;
  return !Schema(*parameters, function.GetBool("strict"), true, 0, &nodes,
                 "schema", nullptr);
}

std::string EncodeSchemaJson(const io::Json& value) {
  const auto encoded = OutputJson(value);
  std::string result;
  result.reserve(encoded.size());
  for (char c : encoded) {
    if (c == '<')
      result += "\\u003c";
    else
      result += c;
  }
  return result;
}

}  // namespace q4t::server
