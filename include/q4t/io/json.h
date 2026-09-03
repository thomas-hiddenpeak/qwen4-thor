// Minimal JSON parser + value type for q4t.
//
// A compact recursive-descent parser covering the JSON grammar (objects,
// arrays, strings with \u escapes, numbers, true/false/null). It is
// deliberately small and dependency-free, and is reused for every JSON file in
// the model directory (safetensors headers, config.json, tokenizer_config.json,
// ...). Not a general-purpose JSON library: no duplicate-key detection beyond
// last-wins, no streaming, numbers are stored as double.
#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

#include "q4t/status.h"

namespace q4t {
namespace io {

struct Json {
  enum class Type { kNull, kBool, kNumber, kString, kArray, kObject };

  Type type = Type::kNull;
  bool boolean = false;
  double number = 0.0;
  std::string str;
  std::vector<Json> array;
  // Object members in insertion order.
  std::vector<std::pair<std::string, Json>> object;

  bool IsNull() const { return type == Type::kNull; }
  bool IsBool() const { return type == Type::kBool; }
  bool IsNumber() const { return type == Type::kNumber; }
  bool IsString() const { return type == Type::kString; }
  bool IsArray() const { return type == Type::kArray; }
  bool IsObject() const { return type == Type::kObject; }

  // Object accessors.
  const Json* Find(const std::string& key) const;
  bool Has(const std::string& key) const { return Find(key) != nullptr; }
  // Returns 0 for a missing/non-number key.
  double GetNumber(const std::string& key, double def = 0.0) const;
  int64_t GetInt(const std::string& key, int64_t def = 0) const;
  std::string GetString(const std::string& key,
                        const std::string& def = "") const;
  bool GetBool(const std::string& key, bool def = false) const;
  const Json* GetArray(const std::string& key) const;

  // Numeric conversions (no-op if not a number).
  int64_t AsInt(int64_t def = 0) const;
  double AsDouble(double def = 0.0) const;
};

// Parse a JSON document. Returns a failure Status with a message on error.
Status ParseJson(const std::string& text, Json* out);

}  // namespace io
}  // namespace q4t
