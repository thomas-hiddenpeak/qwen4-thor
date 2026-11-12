#include "q4t/server/input_snapshot_bindings.h"

#include <algorithm>
#include <charconv>
#include <map>

#include "object_protocol.h"
#include "q4t/server/schema_graph.h"
#include "q4t/server/responses_program.h"

namespace q4t::server {
namespace {
using namespace object_detail;
struct Budget {
  uint64_t bytes = 128ULL << 20;
  size_t values = 262144;
};
Status Hash(const Json& value, Budget* budget, size_t depth,
            std::string* digest) {
  if (depth > 64 || !budget->values || value.str.size() > budget->bytes)
    return Status::Fail("Input identity exceeds bounded work capacity");
  --budget->values;
  budget->bytes -= value.str.size();
  if (value.IsString()) {
    *digest = Digest("s:" + Digest(value.str));
    return {};
  }
  if (!value.IsArray() && !value.IsObject()) {
    std::string text;
    auto status = CanonicalSchemaValue(value, &text);
    if (status) *digest = Digest("v:" + text);
    return status;
  }
  std::string tree = value.IsArray() ? "a:" : "o:";
  if (value.IsArray()) {
    if (value.array.size() > budget->values)
      return Status::Fail("Input identity array exceeds capacity");
    for (const auto& child : value.array) {
      std::string hash;
      auto status = Hash(child, budget, depth + 1, &hash);
      if (!status) return status;
      tree += hash;
    }
  } else {
    if (value.object.size() > budget->values)
      return Status::Fail("Input identity object exceeds capacity");
    std::vector<const std::pair<std::string, Json>*> members;
    for (const auto& member : value.object) members.push_back(&member);
    std::sort(members.begin(), members.end(),
              [](const auto* a, const auto* b) { return a->first < b->first; });
    const std::string* previous = nullptr;
    for (const auto* member : members) {
      if ((previous && *previous == member->first) ||
          member->first.size() > budget->bytes)
        return Status::Fail("Invalid or oversized input object key");
      previous = &member->first;
      budget->bytes -= member->first.size();
      std::string hash;
      auto status = Hash(member->second, budget, depth + 1, &hash);
      if (!status) return status;
      tree += Digest(member->first) + hash;
    }
  }
  *digest = Digest(tree);
  return {};
}
ObjectReply Invalid() {
  return Error(503, "input_snapshot_binding_invalid",
               "Stored input snapshot does not match its original content");
}
const Json* Part(const Json& item, std::string_view path, Json* decoded) {
  if (path.empty() || path.size() > 256 || !path.starts_with('/'))
    return nullptr;
  const Json* current = &item;
  if (item.GetString("type") == "program_output" &&
      path.starts_with("/result/")) {
    if (!DecodeResponsesProgramOutput(item.GetString("result"), decoded))
      return nullptr;
    current = decoded;
    path.remove_prefix(7);
  }
  size_t depth = 0;
  while (!path.empty()) {
    if (++depth > 16 || !path.starts_with('/')) return nullptr;
    path.remove_prefix(1);
    const auto end = path.find('/');
    const auto part = path.substr(0, end);
    if (part.empty()) return nullptr;
    if (current->IsObject()) {
      if (part != "content" && part != "output" && part != "results")
        return nullptr;
      current = current->Find(std::string(part));
      if (!current) return nullptr;
    } else if (current->IsArray()) {
      size_t index = 0;
      const auto [next, error] =
          std::from_chars(part.data(), part.data() + part.size(), index);
      if (error != std::errc{} || next != part.data() + part.size() ||
          (part.size() > 1 && part.front() == '0') ||
          index >= current->array.size())
        return nullptr;
      current = &current->array[index];
    } else {
      return nullptr;
    }
    if (end == path.npos) break;
    path.remove_prefix(end);
  }
  const auto type = current->GetString("type");
  return type == "input_file" || type == "file" || type == "input_image" ||
                 type == "image_url"
             ? current
             : nullptr;
}
struct Binding {
  std::string item, path, digest;
  InputSnapshotRef reference;
};
bool Decode(const Json& value, Binding* binding) {
  const auto* snapshot = value.Find("snapshot");
  if (!value.IsObject() || value.object.size() != 4 || !snapshot ||
      !ParseInputSnapshotReference(*snapshot, &binding->reference))
    return false;
  binding->item = value.GetString("item_id");
  binding->path = value.GetString("path");
  binding->digest = value.GetString("binding_sha256");
  return ValidId(binding->item) && !binding->path.empty() &&
         binding->path.size() <= 256 && binding->digest.size() == 64 &&
         std::all_of(binding->digest.begin(), binding->digest.end(),
                     [](char c) {
                       return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
                     });
}
using Items = std::map<std::string, const Json*>;
bool Index(const Json& items, Items* index) {
  if (!items.IsArray() || items.array.size() > 4096) return false;
  for (const auto& item : items.array) {
    const auto id = item.GetString("id");
    if (!ValidId(id)) return false;
    auto [found, added] = index->emplace(id, &item);
    if (!added) {
      std::string left, right;
      if (!InputValueIdentity(*found->second, &left) ||
          !InputValueIdentity(item, &right) || left != right)
        return false;
    }
  }
  return true;
}
ObjectReply Collect(const Json& bindings, const Items& items, bool subset,
                    std::map<std::pair<std::string, std::string>, Json>* rows) {
  if (bindings.IsNull()) return {};
  if (!bindings.IsArray() || bindings.array.size() > 4096) return Invalid();
  Budget budget;
  for (const auto& row : bindings.array) {
    Binding binding;
    if (!Decode(row, &binding)) return Invalid();
    const auto found = items.find(binding.item);
    if (found == items.end()) {
      if (subset) continue;
      return Invalid();
    }
    Json decoded;
    const auto* part = Part(*found->second, binding.path, &decoded);
    std::string digest;
    if (!part || !Hash(*part, &budget, 0, &digest) || digest != binding.digest)
      return Invalid();
    auto [entry, added] =
        rows->emplace(std::pair{binding.item, binding.path}, row);
    if (!added) {
      Binding previous;
      if (!Decode(entry->second, &previous) ||
          previous.reference.id != binding.reference.id ||
          previous.reference.sha256 != binding.reference.sha256 ||
          previous.digest != binding.digest)
        return Error(
            409, "input_snapshot_binding_conflict",
            "The same original part has different immutable snapshots");
    }
    if (rows->size() > 4096) return Invalid();
  }
  return {};
}
}  // namespace

Status InputValueIdentity(const Json& value, std::string* sha256,
                          size_t max_values) {
  if (!sha256) return Status::Fail("Missing input identity output");
  sha256->clear();
  Budget budget;
  if (!max_values || max_values > 400000)
    return Status::Fail("Invalid input identity value capacity");
  budget.values = max_values;
  return Hash(value, &budget, 0, sha256);
}
ObjectReply BindInputSnapshot(const Json& item, std::string_view path,
                              const InputSnapshotRef& reference,
                              Json* binding) {
  if (!binding) return Invalid();
  *binding = {};
  Json decoded;
  const auto* part = Part(item, path, &decoded);
  InputSnapshotRef parsed;
  std::string digest;
  if (!part || !ValidId(item.GetString("id")) ||
      !ParseInputSnapshotReference(InputSnapshotReference(reference),
                                   &parsed) ||
      !InputValueIdentity(*part, &digest))
    return Invalid();
  *binding = Object({{"item_id", String(item.GetString("id"))},
                     {"path", String(std::string(path))},
                     {"binding_sha256", String(digest)},
                     {"snapshot", InputSnapshotReference(reference)}});
  return {};
}
ObjectReply MergeInputSnapshots(const Json& added, const Json& items,
                                Json* bindings) {
  if (!bindings) return Invalid();
  if ((bindings->IsNull() ||
       (bindings->IsArray() && bindings->array.empty())) &&
      (added.IsNull() || (added.IsArray() && added.array.empty()))) {
    *bindings = Array();
    return {};
  }
  Items index;
  if (!Index(items, &index)) return Invalid();
  std::map<std::pair<std::string, std::string>, Json> rows;
  auto reply = Collect(*bindings, index, false, &rows);
  if (reply.ok()) reply = Collect(added, index, false, &rows);
  if (!reply.ok()) return reply;
  auto result = Array();
  for (auto& [key, row] : rows) result.array.push_back(std::move(row));
  *bindings = std::move(result);
  return {};
}
ObjectReply SelectInputSnapshots(const Json& bindings, const Json& items,
                                 Json* selected) {
  if (!selected) return Invalid();
  Items index;
  if (!Index(items, &index)) return Invalid();
  std::map<std::pair<std::string, std::string>, Json> rows;
  auto reply = Collect(bindings, index, true, &rows);
  if (!reply.ok()) return reply;
  auto result = Array();
  for (auto& [key, row] : rows) result.array.push_back(std::move(row));
  *selected = std::move(result);
  return {};
}
}  // namespace q4t::server
