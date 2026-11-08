#include "q4t/server/schema_graph.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <limits>
#include <vector>

#include "q4t/server/input_contract.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/schema_decimal.h"
#include "q4t/server/schema_prefix.h"

namespace q4t::server {
namespace {
using io::Json;
constexpr size_t kWork = 8 * 1024 * 1024;

Status Charge(size_t amount, size_t* work) {
  if (amount > *work)
    return Status::Fail("schema_resource_exhausted: validation work limit");
  *work -= amount;
  return Status();
}
size_t Characters(std::string_view text) {
  return std::count_if(text.begin(), text.end(),
                       [](unsigned char c) { return (c & 0xc0) != 0x80; });
}
std::vector<std::string> Types(const Json& schema) {
  const auto* value = schema.Find("type");
  if (!value) return {};
  if (value->IsString()) return {value->str};
  std::vector<std::string> result;
  if (value->IsArray())
    for (const auto& item : value->array)
      if (item.IsString()) result.push_back(item.str);
  return result;
}
bool Has(const std::vector<std::string>& types, std::string_view value) {
  return std::find(types.begin(), types.end(), value) != types.end();
}
Status Number(const Json& value, SchemaDecimal* out) {
  if (!value.IsNumber() || !std::isfinite(value.number))
    return Status::Fail("expected finite JSON number");
  return SchemaDecimal::Parse(OutputJson(value), out);
}
bool Matches(const Json& value, std::string_view type) {
  if (type == "string") return value.IsString();
  if (type == "number") return value.IsNumber();
  if (type == "boolean") return value.IsBool();
  if (type == "null") return value.IsNull();
  if (type == "object") return value.IsObject();
  if (type == "array") return value.IsArray();
  if (type == "integer") {
    SchemaDecimal decimal;
    return Number(value, &decimal).ok() && decimal.IsInteger();
  }
  return false;
}

// Canonical equality follows JSON values, including object member order and
// exact mathematical number equality (1 == 1.0, without 2^53 rounding).
Status Key(const Json& value, std::string* out, size_t* work,
           size_t depth = 0) {
  auto status = Charge(1 + value.str.size(), work);
  if (!status) return status;
  if (depth > 64) return Status::Fail("schema_resource_exhausted: value depth");
  if (value.IsNumber()) {
    SchemaDecimal decimal;
    status = Number(value, &decimal);
    if (status) *out += decimal.Canonical();
    return status;
  }
  if (!value.IsArray() && !value.IsObject()) {
    *out += OutputJson(value);
    return Status();
  }
  *out += value.IsArray() ? '[' : '{';
  if (value.IsArray()) {
    for (const auto& item : value.array) {
      status = Key(item, out, work, depth + 1);
      if (!status) return status;
      *out += ',';
    }
  } else {
    std::vector<const std::pair<std::string, Json>*> members;
    for (const auto& member : value.object) members.push_back(&member);
    std::sort(members.begin(), members.end(),
              [](const auto* a, const auto* b) { return a->first < b->first; });
    std::string previous;
    bool first = true;
    for (const auto* member : members) {
      if (!first && previous == member->first)
        return Status::Fail("duplicate JSON object member");
      previous = member->first;
      first = false;
      status = Charge(member->first.size(), work);
      if (!status) return status;
      *out += std::to_string(member->first.size()) + ':' + member->first;
      status = Key(member->second, out, work, depth + 1);
      if (!status) return status;
      *out += ',';
    }
  }
  *out += value.IsArray() ? ']' : '}';
  return Status();
}

Status PartialEqual(const Json& expected, const SchemaPrefixValue& prefix,
                    size_t* work, bool* matches, size_t depth = 0) {
  *matches = true;
  auto status = Charge(1, work);
  if (!status) return status;
  if (depth > 64)
    return Status::Fail("schema_resource_exhausted: prefix enum depth");
  if (!prefix.started) return {};
  const auto equal = [&](const Json& a, const Json& b, bool* result) {
    std::string left, right;
    auto status = Key(a, &left, work);
    if (status) status = Key(b, &right, work);
    if (status) *result = left == right;
    return status;
  };
  const auto& value = prefix.value;
  if (prefix.complete) return equal(expected, value, matches);
  if (expected.type != value.type) {
    *matches = false;
    return {};
  }
  if (value.IsString()) {
    status = Charge(value.str.size(), work);
    *matches = expected.str.starts_with(value.str);
    return status;
  }
  if (value.IsBool()) {
    *matches = expected.boolean == value.boolean;
    return {};
  }
  if (value.IsArray()) {
    if (expected.array.size() < value.array.size() + (prefix.pending ? 1 : 0)) {
      *matches = false;
      return {};
    }
    for (size_t i = 0; i < value.array.size(); ++i) {
      status = equal(expected.array[i], value.array[i], matches);
      if (!status || !*matches) return status;
    }
    if (prefix.pending)
      return PartialEqual(expected.array[value.array.size()], *prefix.pending,
                          work, matches, depth + 1);
  }
  if (value.IsObject()) {
    for (const auto& [key, child] : value.object) {
      const auto* candidate = expected.Find(key);
      if (!candidate) {
        *matches = false;
        return {};
      }
      status = equal(*candidate, child, matches);
      if (!status || !*matches) return status;
    }
    if (prefix.has_pending_key) {
      const auto* candidate = expected.Find(prefix.pending_key);
      if (!candidate) {
        *matches = false;
        return {};
      }
      if (prefix.pending)
        return PartialEqual(*candidate, *prefix.pending, work, matches,
                            depth + 1);
    }
  }
  // An incomplete number may still gain an exponent. Exact equality is
  // checked at its delimiter; no provisional binary64 value is accepted.
  return {};
}

int Hex(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}
const Json* Pointer(const Json& root, std::string_view reference) {
  if (!reference.starts_with('#')) return nullptr;
  std::string pointer;
  for (size_t i = 1; i < reference.size(); ++i) {
    if (reference[i] != '%') {
      pointer += reference[i];
    } else {
      if (i + 2 >= reference.size() || Hex(reference[i + 1]) < 0 ||
          Hex(reference[i + 2]) < 0)
        return nullptr;
      pointer += static_cast<char>((Hex(reference[i + 1]) << 4) |
                                   Hex(reference[i + 2]));
      i += 2;
    }
  }
  const Json* current = &root;
  if (pointer.empty()) return current;
  if (pointer[0] != '/') return nullptr;
  for (size_t begin = 1;;) {
    auto end = pointer.find('/', begin);
    if (end == pointer.npos) end = pointer.size();
    std::string name;
    for (size_t i = begin; i < end; ++i) {
      if (pointer[i] != '~')
        name += pointer[i];
      else {
        if (++i == end || (pointer[i] != '0' && pointer[i] != '1'))
          return nullptr;
        name += pointer[i] == '0' ? '~' : '/';
      }
    }
    if (current->IsObject()) {
      current = current->Find(name);
    } else if (current->IsArray()) {
      size_t index = 0;
      const auto parsed =
          std::from_chars(name.data(), name.data() + name.size(), index);
      if (name.empty() || (name.size() > 1 && name[0] == '0') ||
          parsed.ec != std::errc{} || parsed.ptr != name.data() + name.size() ||
          index >= current->array.size())
        return nullptr;
      current = &current->array[index];
    } else {
      return nullptr;
    }
    if (!current || end == pointer.size()) return current;
    begin = end + 1;
  }
}

std::vector<const Json*> Children(const Json& schema) {
  std::vector<const Json*> result;
  if (const auto* properties = schema.Find("properties"))
    for (const auto& [name, child] : properties->object)
      result.push_back(&child);
  if (const auto* items = schema.Find("items")) result.push_back(items);
  return result;
}
}  // namespace

Status OutputSchemaGraph::Error(const std::string& path,
                                const std::string& message) {
  if (error_param_) *error_param_ = path;
  return Status::Fail(message);
}

Status OutputSchemaGraph::Collect(const Json& schema, const std::string& path,
                                  size_t depth) {
  if (!schema.IsObject()) return Error(path, "schema must be an object");
  if (depth > 64 || nodes_.size() >= 20000)
    return Error(path, "schema_resource_exhausted: schema graph capacity");
  nodes_[&schema] = path;
  order_.push_back(&schema);
  for (const auto* field : {"properties", "$defs"}) {
    const auto* object = schema.Find(field);
    if (!object) continue;
    if (!object->IsObject())
      return Error(InputParamPath(path, field), "expected schema map");
    if (std::string_view(field) == "properties") {
      property_count_ += object->object.size();
      if (property_count_ > 5000)
        return Error(path + ".properties", "schema exceeds 5000 properties");
    }
    std::unordered_set<std::string> names;
    for (const auto& [name, child] : object->object) {
      string_count_ += Characters(name);
      const auto child_path = InputParamPath(InputParamPath(path, field), name);
      if (!names.insert(name).second)
        return Error(child_path, "duplicate schema name");
      auto status = Collect(child, child_path, depth + 1);
      if (!status) return status;
    }
  }
  if (const auto* array = schema.Find("anyOf")) {
    if (!array->IsArray() || array->array.empty())
      return Error(path + ".anyOf", "anyOf requires nonempty schemas");
    for (size_t i = 0; i < array->array.size(); ++i) {
      auto status =
          Collect(array->array[i], path + ".anyOf[" + std::to_string(i) + ']',
                  depth + 1);
      if (!status) return status;
    }
  }
  if (const auto* items = schema.Find("items")) {
    auto status = Collect(*items, path + ".items", depth + 1);
    if (!status) return status;
  }
  return Status();
}

Status OutputSchemaGraph::Definition(const Json& schema,
                                     const std::string& path) {
  const auto types = Types(schema);
  if (const auto* type = schema.Find("type")) {
    if (types.empty() ||
        (type->IsArray() && types.size() != type->array.size()))
      return Error(path + ".type", "type must name JSON types");
    std::unordered_set<std::string> seen;
    for (size_t i = 0; i < types.size(); ++i) {
      const auto& name = types[i];
      if ((name != "null" && name != "boolean" && name != "integer" &&
           name != "number" && name != "string" && name != "object" &&
           name != "array") ||
          !seen.insert(name).second)
        return Error(path + ".type" +
                         (type->IsArray() ? '[' + std::to_string(i) + ']' : ""),
                     "invalid or duplicate schema type");
    }
  } else if (!schema.Has("$ref") && !schema.Has("anyOf") &&
             !schema.Has("enum") && !schema.Has("const")) {
    return Error(path + ".type", "schema requires type, reference or union");
  }
  std::unordered_set<std::string> fields;
  for (const auto& [key, value] : schema.object) {
    const auto location = InputParamPath(path, key);
    if (!fields.insert(key).second) return Error(location, "duplicate keyword");
    if (key == "type" || key == "anyOf" || key == "$defs") continue;
    if (key == "$ref") {
      const auto* target =
          value.IsString() ? Pointer(*root_, value.str) : nullptr;
      if (!target || !nodes_.contains(target))
        return Error(location, "reference must resolve to a local schema");
      references_[&schema] = target;
    } else if (key == "title" || key == "description" || key == "$comment" ||
               key == "$schema") {
      if (!value.IsString())
        return Error(location, "annotation must be string");
    } else if (key == "default" || key == "examples") {
      // Annotations do not supply missing values or override constraints.
      if (key == "examples" && !value.IsArray())
        return Error(location, "examples must be array");
    } else if (key == "properties" || key == "required" ||
               key == "additionalProperties") {
      if (!types.empty() && !Has(types, "object"))
        return Error(location, "object keyword on non-object type");
      if (key == "additionalProperties" && (!value.IsBool() || value.boolean))
        return Error(location, "additionalProperties must be false");
      if (key == "required") {
        if (!value.IsArray()) return Error(location, "required must be array");
        std::unordered_set<std::string> names;
        for (const auto& name : value.array)
          if (!name.IsString() || !names.insert(name.str).second)
            return Error(location, "required must contain unique strings");
      }
    } else if (key == "items" || key == "minItems" || key == "maxItems") {
      if (!types.empty() && !Has(types, "array"))
        return Error(location, "array keyword on non-array type");
    } else if (key == "minLength" || key == "maxLength" || key == "pattern" ||
               key == "format") {
      if (!types.empty() && !Has(types, "string"))
        return Error(location, "string keyword on non-string type");
      if ((key == "pattern" || key == "format") && !value.IsString())
        return Error(location, "string constraint must be string");
      if (key == "format" && !Has({"date-time", "time", "date", "duration",
                                   "email", "hostname", "ipv4", "ipv6", "uuid"},
                                  value.str))
        return Error(location, "unknown string format");
    } else if (key == "minimum" || key == "maximum" ||
               key == "exclusiveMinimum" || key == "exclusiveMaximum" ||
               key == "multipleOf") {
      if (!types.empty() && !Has(types, "integer") && !Has(types, "number"))
        return Error(location, "numeric keyword on non-numeric type");
      SchemaDecimal number;
      auto status = Number(value, &number);
      if (!status) return Error(location, status.message());
      if (key == "multipleOf" && (number.Negative() || number.IsZero()))
        return Error(location, "multipleOf must be positive");
    } else if (key == "enum" || key == "const") {
      std::vector<const Json*> values;
      if (key == "enum") {
        if (!value.IsArray() || value.array.empty())
          return Error(location, "enum requires nonempty values");
        enum_count_ += value.array.size();
        if (enum_count_ > 1000)
          return Error(location, "enum exceeds 1000 values");
        for (const auto& item : value.array) values.push_back(&item);
      } else {
        values.push_back(&value);
      }
      std::unordered_set<std::string> seen;
      size_t characters = 0, work = kWork;
      for (const auto* item : values) {
        if (!types.empty() &&
            !std::any_of(types.begin(), types.end(), [&](const auto& type) {
              return Matches(*item, type);
            }))
          return Error(location, "enum/const value does not match its type");
        std::string canonical;
        auto status = Key(*item, &canonical, &work);
        if (!status) return Error(location, status.message());
        if (!seen.insert(canonical).second)
          return Error(location, "duplicate mathematical enum value");
        characters +=
            Characters(item->IsString() ? item->str : OutputJson(*item));
      }
      string_count_ += characters;
      if (key == "enum" && values.size() > 250 && characters > 15000)
        return Error(location, "large enum exceeds 15000 characters");
    } else {
      return Error(location, "keyword outside the supported schema dialect");
    }
    if (key == "minItems" || key == "maxItems" || key == "minLength" ||
        key == "maxLength") {
      SchemaDecimal number;
      auto status = Number(value, &number);
      if (!status || !number.IsInteger() || number.Negative())
        return Error(location, "length bound must be a nonnegative integer");
      if (value.number > std::numeric_limits<int32_t>::max())
        return Error(location,
                     "schema_resource_exhausted: length counter range");
    }
  }
  if (string_count_ > 120000)
    return Error(path, "schema exceeds 120000 constrained characters");
  if (schema.Has("properties") ||
      (Has(types, "object") && !schema.Has("$ref") && !schema.Has("anyOf"))) {
    const auto* properties = schema.Find("properties");
    if (!properties || !properties->IsObject())
      return Error(path + ".properties", "object requires properties map");
    const auto* additional = schema.Find("additionalProperties");
    if (!additional || !additional->IsBool() || additional->boolean)
      return Error(path + ".additionalProperties", "object must be closed");
    std::unordered_set<std::string> required;
    if (const auto* list = schema.Find("required")) {
      if (!list->IsArray()) return Error(path + ".required", "expected array");
      for (size_t i = 0; i < list->array.size(); ++i) {
        const auto& item = list->array[i];
        if (!item.IsString() || !properties->Has(item.str) ||
            !required.insert(item.str).second)
          return Error(path + ".required[" + std::to_string(i) + ']',
                       "required must name unique declared properties");
      }
    }
    if (strict_ && required.size() != properties->object.size())
      return Error(path + ".required", "strict objects require every property");
  }
  if (Has(types, "array") && !schema.Has("items"))
    return Error(path + ".items", "array requires item schema");
  for (const auto* suffix : {"Items", "Length"}) {
    const auto* minimum = schema.Find(std::string("min") + suffix);
    const auto* maximum = schema.Find(std::string("max") + suffix);
    if (minimum && maximum && minimum->number > maximum->number)
      return Error(path + ".min" + suffix, "minimum length exceeds maximum");
  }
  return Status();
}

const Json* OutputSchemaGraph::RefTarget(const Json& schema) const {
  const auto found = references_.find(&schema);
  return found == references_.end() ? nullptr : found->second;
}

Status OutputSchemaGraph::Depth(const Json& schema, size_t depth,
                                std::unordered_set<const Json*>* active,
                                std::unordered_map<const Json*, size_t>* seen) {
  if (active->contains(&schema)) return Status();  // Productive recursion.
  if (depth > 10) return Error(nodes_.at(&schema), "schema exceeds 10 levels");
  if (const auto prior = seen->find(&schema);
      prior != seen->end() && prior->second >= depth)
    return Status();
  (*seen)[&schema] = depth;
  active->insert(&schema);
  if (const auto* target = RefTarget(schema)) {
    auto status = Depth(*target, depth, active, seen);
    if (!status) return status;
  }
  if (const auto* choices = schema.GetArray("anyOf")) {
    for (const auto& child : choices->array) {
      auto status = Depth(child, depth, active, seen);
      if (!status) return status;
    }
  }
  for (const auto* child : Children(schema)) {
    auto status = Depth(*child, depth + 1, active, seen);
    if (!status) return status;
  }
  active->erase(&schema);
  return Status();
}

Status OutputSchemaGraph::Init(const Json& root, bool strict,
                               std::string* error_param,
                               std::string_view path) {
  *this = {};
  root_ = &root;
  strict_ = strict;
  error_param_ = error_param;
  if (error_param_) error_param_->clear();
  auto status = Collect(root, std::string(path), 0);
  if (!status) return status;
  for (const auto* schema : order_) {
    status = Definition(*schema, nodes_.at(schema));
    if (!status) return status;
  }
  const Json* effective = &root;
  std::unordered_set<const Json*> chain;
  while (effective->Has("$ref")) {
    if (!chain.insert(effective).second)
      return Error(InputParamPath(path, "$ref"),
                   "nonproductive root reference");
    effective = RefTarget(*effective);
  }
  if (effective->GetString("type") != "object" || effective->Has("anyOf"))
    return Error(std::string(path) + ".type",
                 "root must be an object, not anyOf");
  std::unordered_set<const Json*> active;
  std::unordered_map<const Json*, size_t> seen;
  status = Depth(root, 0, &active, &seen);
  if (!status) return status;
  valid_ = true;
  error_param_ = nullptr;
  return Status();
}

Status OutputSchemaGraph::Value(const Json& schema, const Json& value,
                                size_t depth, size_t* work,
                                const StringCheck& strings,
                                std::unordered_set<const Json*>* active) const {
  auto status = Charge(1, work);
  if (!status) return status;
  if (depth > 64)
    return Status::Fail("schema_resource_exhausted: output depth");
  if (!active->insert(&schema).second)
    return Status::Fail("schema recursion without value progress");
  struct Pop {
    std::unordered_set<const Json*>* set;
    const Json* key;
    ~Pop() { set->erase(key); }
  } pop{active, &schema};
  if (const auto* target = RefTarget(schema)) {
    status = Value(*target, value, depth, work, strings, active);
    if (!status) return status;
  }
  if (const auto* choices = schema.GetArray("anyOf")) {
    bool matched = false;
    for (const auto& child : choices->array) {
      status = Value(child, value, depth, work, strings, active);
      if (status) {
        matched = true;
        break;
      }
      if (status.message().starts_with("schema_resource_exhausted:") ||
          status.message().starts_with("schema_dependency_unavailable:"))
        return status;
    }
    if (!matched) return Status::Fail("output does not match anyOf");
  }
  const auto types = Types(schema);
  if (!types.empty() &&
      !std::any_of(types.begin(), types.end(),
                   [&](const auto& type) { return Matches(value, type); }))
    return Status::Fail("output type does not match schema");
  if (schema.Has("const") || schema.Has("enum")) {
    std::string actual;
    status = Key(value, &actual, work);
    if (!status) return status;
    if (const auto* expected = schema.Find("const")) {
      std::string key;
      status = Key(*expected, &key, work);
      if (!status) return status;
      if (actual != key) return Status::Fail("output does not match const");
    }
    if (const auto* values = schema.GetArray("enum")) {
      bool found = false;
      for (const auto& item : values->array) {
        std::string key;
        status = Key(item, &key, work);
        if (!status) return status;
        if (key == actual) {
          found = true;
          break;
        }
      }
      if (!found) return Status::Fail("output is not a declared enum value");
    }
  }
  if (value.IsNumber()) {
    SchemaDecimal actual;
    status = Number(value, &actual);
    if (!status) return status;
    for (const auto* key : {"minimum", "maximum", "exclusiveMinimum",
                            "exclusiveMaximum", "multipleOf"}) {
      const auto* raw = schema.Find(key);
      if (!raw) continue;
      SchemaDecimal bound;
      status = Number(*raw, &bound);
      if (!status) return status;
      const int cmp = actual.Compare(bound);
      bool accepted = true;
      if (std::string_view(key) == "minimum") accepted = cmp >= 0;
      if (std::string_view(key) == "maximum") accepted = cmp <= 0;
      if (std::string_view(key) == "exclusiveMinimum") accepted = cmp > 0;
      if (std::string_view(key) == "exclusiveMaximum") accepted = cmp < 0;
      if (std::string_view(key) == "multipleOf") {
        status = actual.MultipleOf(bound, &accepted, work);
        if (!status) return status;
      }
      if (!accepted) return Status::Fail(std::string("output violates ") + key);
    }
  }
  if (value.IsString()) {
    const auto count = Characters(value.str);
    status = Charge(value.str.size(), work);
    if (!status) return status;
    if (count < static_cast<size_t>(schema.GetInt("minLength", 0)) ||
        (schema.Has("maxLength") &&
         count > static_cast<size_t>(schema.GetInt("maxLength"))))
      return Status::Fail("output string length violates schema");
    if (schema.Has("pattern") || schema.Has("format")) {
      if (!strings) return Status::Fail("schema string checker is required");
      bool accepted = false;
      status = strings(schema, value.str, work, &accepted);
      if (!status) return status;
      if (!accepted)
        return Status::Fail("output string violates pattern/format");
    }
  }
  if (value.IsArray()) {
    if (value.array.size() <
            static_cast<size_t>(schema.GetInt("minItems", 0)) ||
        (schema.Has("maxItems") &&
         value.array.size() > static_cast<size_t>(schema.GetInt("maxItems"))))
      return Status::Fail("output array length violates schema");
    if (const auto* items = schema.Find("items"))
      for (const auto& item : value.array) {
        std::unordered_set<const Json*> child_active;
        status = Value(*items, item, depth + 1, work, strings, &child_active);
        if (!status) return status;
      }
  }
  if (value.IsObject()) {
    if (const auto* required = schema.GetArray("required"))
      for (const auto& key : required->array)
        if (!value.Has(key.str))
          return Status::Fail("missing required property");
    const auto* properties = schema.Find("properties");
    const bool closed = schema.Has("additionalProperties") &&
                        !schema.GetBool("additionalProperties");
    std::unordered_set<std::string> seen;
    for (const auto& [key, item] : value.object) {
      status = Charge(key.size(), work);
      if (!status) return status;
      const auto* child = properties ? properties->Find(key) : nullptr;
      if (!seen.insert(key).second || (!child && closed))
        return Status::Fail("unknown or duplicate output property");
      if (child) {
        std::unordered_set<const Json*> child_active;
        status = Value(*child, item, depth + 1, work, strings, &child_active);
        if (!status) return status;
      }
    }
  }
  return Status();
}

Status OutputSchemaGraph::Check(const Json& value,
                                const StringCheck& strings) const {
  if (!valid_) return Status::Fail("schema graph is not initialized");
  return CheckNode(*root_, value, strings);
}

Status OutputSchemaGraph::Prefix(
    const Json& schema, const SchemaPrefixValue& prefix, size_t depth,
    size_t* work, const StringCheck& strings,
    std::unordered_set<const Json*>* active) const {
  if (prefix.complete)
    return Value(schema, prefix.value, depth, work, strings, active);
  auto status = Charge(1, work);
  if (!status) return status;
  if (depth > 64)
    return Status::Fail("schema_resource_exhausted: prefix depth");
  if (!prefix.started) return {};
  if (!active->insert(&schema).second)
    return Status::Fail("schema recursion without prefix progress");
  struct Pop {
    std::unordered_set<const Json*>* set;
    const Json* key;
    ~Pop() { set->erase(key); }
  } pop{active, &schema};
  const auto& value = prefix.value;
  const auto types = Types(schema);
  if (!types.empty() &&
      !std::any_of(types.begin(), types.end(), [&](const auto& type) {
        return (value.IsNumber() && (type == "number" || type == "integer")) ||
               Matches(value, type);
      }))
    return Status::Fail("prefix type does not match schema");
  if (const auto* target = RefTarget(schema)) {
    status = Prefix(*target, prefix, depth, work, strings, active);
    if (!status) return status;
  }
  if (const auto* choices = schema.GetArray("anyOf")) {
    bool found = false;
    for (const auto& child : choices->array) {
      status = Prefix(child, prefix, depth, work, strings, active);
      if (status) {
        found = true;
        break;
      }
      if (status.message().starts_with("schema_resource_exhausted:") ||
          status.message().starts_with("schema_dependency_unavailable:"))
        return status;
    }
    if (!found) return Status::Fail("prefix does not match anyOf");
  }
  if (const auto* expected = schema.Find("const")) {
    bool matches;
    status = PartialEqual(*expected, prefix, work, &matches);
    if (!status) return status;
    if (!matches) return Status::Fail("prefix does not match const");
  }
  if (const auto* choices = schema.GetArray("enum")) {
    bool found = false;
    for (const auto& candidate : choices->array) {
      bool matches;
      status = PartialEqual(candidate, prefix, work, &matches);
      if (!status) return status;
      if (matches) {
        found = true;
        break;
      }
    }
    if (!found) return Status::Fail("prefix does not match enum");
  }
  if (value.IsString() && schema.Has("maxLength") &&
      Characters(value.str) > static_cast<size_t>(schema.GetInt("maxLength")))
    return Status::Fail("prefix exceeds string length");
  if (value.IsArray()) {
    const auto count = value.array.size() + (prefix.pending ? 1 : 0);
    if (schema.Has("maxItems") &&
        count > static_cast<size_t>(schema.GetInt("maxItems")))
      return Status::Fail("prefix exceeds array length");
    if (const auto* items = schema.Find("items")) {
      for (const auto& child : value.array) {
        std::unordered_set<const Json*> child_active;
        status = Value(*items, child, depth + 1, work, strings, &child_active);
        if (!status) return status;
      }
      if (prefix.pending) {
        std::unordered_set<const Json*> child_active;
        status = Prefix(*items, *prefix.pending, depth + 1, work, strings,
                        &child_active);
        if (!status) return status;
      }
    }
  }
  if (value.IsObject()) {
    const auto* properties = schema.Find("properties");
    const bool closed = schema.Has("additionalProperties") &&
                        !schema.GetBool("additionalProperties");
    for (const auto& [key, child] : value.object) {
      const auto* node = properties ? properties->Find(key) : nullptr;
      if (!node && closed) return Status::Fail("unknown prefix property");
      if (node) {
        std::unordered_set<const Json*> child_active;
        status = Value(*node, child, depth + 1, work, strings, &child_active);
        if (!status) return status;
      }
    }
    if (prefix.has_pending_key) {
      const auto* node =
          properties ? properties->Find(prefix.pending_key) : nullptr;
      if (!node && closed) return Status::Fail("unknown pending property");
      if (node && prefix.pending) {
        std::unordered_set<const Json*> child_active;
        status = Prefix(*node, *prefix.pending, depth + 1, work, strings,
                        &child_active);
        if (!status) return status;
      }
    }
  }
  return {};
}

Status OutputSchemaGraph::CheckPrefix(const SchemaPrefixValue& value,
                                      size_t* work_remaining,
                                      const StringCheck& strings) const {
  if (!valid_) return Status::Fail("schema graph is not initialized");
  if (!work_remaining)
    return Status::Fail(
        "schema_resource_exhausted: prefix work budget missing");
  std::unordered_set<const Json*> active;
  return Prefix(*root_, value, 0, work_remaining, strings, &active);
}

Status OutputSchemaGraph::CheckNode(const Json& schema, const Json& value,
                                    const StringCheck& strings) const {
  if (!valid_ || !nodes_.contains(&schema))
    return Status::Fail("schema node is not initialized");
  size_t work = kWork;
  std::unordered_set<const Json*> active;
  return Value(schema, value, 0, &work, strings, &active);
}
}  // namespace q4t::server
