// Internal builders; external values must still pass contract validation.
#pragma once

#include <initializer_list>
#include <string>
#include <string_view>
#include <utility>

#include "q4t/io/json.h"

namespace q4t::server::responses_detail {
using io::Json;
inline Json String(std::string value) {
  Json result;
  result.type = Json::Type::kString;
  result.str = std::move(value);
  return result;
}
inline Json Number(int64_t value) {
  Json result;
  result.type = Json::Type::kNumber;
  result.number = static_cast<double>(value);
  result.str = std::to_string(value);
  return result;
}
inline Json Boolean(bool value) {
  Json result;
  result.type = Json::Type::kBool;
  result.boolean = value;
  return result;
}
inline Json Object(
    std::initializer_list<std::pair<std::string, Json>> fields = {}) {
  Json result;
  result.type = Json::Type::kObject;
  result.object = fields;
  return result;
}
inline Json Array(std::initializer_list<Json> items = {}) {
  Json result;
  result.type = Json::Type::kArray;
  result.array = items;
  return result;
}
inline Json& Field(Json& object, std::string_view key) {
  for (auto& [name, item] : object.object)
    if (name == key) return item;
  object.object.emplace_back(key, Json());
  return object.object.back().second;
}
inline void Set(Json* object, std::string_view key, Json value) {
  Field(*object, key) = std::move(value);
}
inline bool Nonnull(const Json& object, const std::string& key) {
  const auto* value = object.Find(key);
  return value && !value->IsNull();
}
}  // namespace q4t::server::responses_detail
