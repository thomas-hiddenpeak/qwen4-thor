#include "q4t/server/object_retention.h"

#include <algorithm>
#include <deque>
#include <unordered_map>
#include <unordered_set>

#include "q4t/server/application_routes.h"
#include "q4t/server/application_payload.h"
#include "q4t/server/binary_store.h"
#include "q4t/server/batch_state.h"
#include "q4t/server/input_snapshot.h"
#include "q4t/trace/sha256.h"

namespace q4t::server {
namespace {
using io::Json;
constexpr size_t kReferenceBytes = 1024 * 1024;
constexpr size_t kPlanBytes = 64 * 1024 * 1024;

class Inspector {
 public:
  ObjectRetentionInfo value;

  void Reference(const Json* field) {
    if (!field || field->IsNull()) return;
    if (!field->IsString() || !ValidApplicationId(field->str)) {
      value.uncertain = true;
      return;
    }
    if (reference_bytes_ >
        kReferenceBytes - field->str.size() - sizeof(std::string)) {
      value.uncertain = true;
      return;
    }
    reference_bytes_ += field->str.size() + sizeof(std::string);
    value.references.push_back(field->str);
  }

  void Items(const Json* items, size_t depth = 0) {
    if (!items || items->IsNull() || items->IsString()) return;
    if (depth > 64 || ++work_ > 2 * 1024 * 1024) {
      value.uncertain = true;
      return;
    }
    if (items->IsArray()) {
      for (const auto& item : items->array) Items(&item, depth + 1);
      return;
    }
    if (!items->IsObject()) {
      value.uncertain = true;
      return;
    }
    Reference(items->Find("id"));
    Reference(items->Find("file_id"));
    // Traverse typed item/content containers, not metadata, arbitrary tool
    // arguments, text, or user-authored JSON strings that look like IDs.
    for (const auto* key : {"content", "content_parts", "output", "results"})
      Items(items->Find(key), depth + 1);
    if (const auto* file = items->Find("file"); file && file->IsObject())
      Reference(file->Find("file_id"));
  }

  void Request(const Json* request) {
    if (!request || request->IsNull()) return;
    if (!request->IsObject()) {
      value.uncertain = true;
      return;
    }
    Reference(request->Find("previous_response_id"));
    if (const auto* conversation = request->Find("conversation"))
      Reference(conversation->IsObject() ? conversation->Find("id")
                                         : conversation);
    if (const auto* prompt = request->Find("prompt");
        prompt && prompt->IsObject())
      Reference(prompt->Find("id"));
    Items(request->Find("input"));
    Items(request->Find("messages"));
  }

  void Blob(const Json& data) {
    const auto* blob = data.Find("blob");
    BinaryManifest manifest;
    if (!blob || !ParseBinaryManifest(*blob, &manifest)) {
      value.uncertain = true;
      return;
    }
    const auto* chunks = blob->GetArray("chunks");
    for (const auto& chunk : chunks->array) Reference(chunk.Find("id"));
  }

  void Snapshots(const Json& data) {
    const auto* snapshots = data.Find("input_snapshots");
    if (!snapshots) return;  // Records written before file input support.
    if (!snapshots->IsArray() || snapshots->array.size() > 4096) {
      value.uncertain = true;
      return;
    }
    for (const auto& binding : snapshots->array) {
      const auto* snapshot = binding.Find("snapshot");
      InputSnapshotRef reference;
      if (!snapshot || !ParseInputSnapshotReference(*snapshot, &reference)) {
        value.uncertain = true;
        return;
      }
      Reference(snapshot->Find("id"));
    }
  }

  void Payloads(const Json& data) {
    std::vector<ApplicationStringRef> references;
    if (!ParseApplicationPayloadReferences(data, &references)) {
      value.uncertain = true;
      return;
    }
    for (const auto& ref : references) {
      Json id;
      id.type = Json::Type::kString;
      id.str = ref.snapshot.id;
      Reference(&id);
    }
  }

  ObjectRetentionInfo Finish() {
    auto& references = value.references;
    std::sort(references.begin(), references.end());
    references.erase(std::unique(references.begin(), references.end()),
                     references.end());
    if (value.Bytes() > kReferenceBytes) value.uncertain = true;
    if (value.uncertain) {
      // Incomplete reference extraction conservatively protects this owner.
      std::vector<std::string>().swap(references);
    }
    return std::move(value);
  }

 private:
  size_t reference_bytes_ = 0;
  size_t work_ = 0;
};

std::string Key(std::string_view owner, std::string_view id) {
  return std::string(owner) + ':' + std::string(id);
}
}  // namespace

size_t ObjectRetentionInfo::Bytes() const {
  size_t bytes = references.capacity() * sizeof(std::string);
  for (const auto& reference : references) bytes += reference.capacity() + 1;
  return bytes;
}

ObjectRetentionInfo InspectObjectRetention(std::string_view kind,
                                           const io::Json& data) {
  Inspector inspector;
  if (!data.IsObject()) {
    inspector.value.uncertain = true;
    return inspector.Finish();
  }
  if (kind == "response") {
    if (const auto* response = data.Find("response");
        response && response->IsObject()) {
      const auto status = response->GetString("status");
      inspector.value.active = status == "queued" || status == "in_progress";
      inspector.value.uncertain =
          !inspector.value.active && status != "completed" &&
          status != "incomplete" && status != "failed" && status != "cancelled";
      inspector.Items(response->Find("output"));
    } else {
      inspector.value.uncertain = true;
    }
    inspector.Request(data.Find("request"));
    inspector.Items(data.Find("history"));
    inspector.Items(data.Find("own_input"));
    if (const auto* continuation = data.Find("continuation")) {
      const auto* id = continuation->Find("conversation_id");
      if (id && id->IsString() && !id->str.empty()) inspector.Reference(id);
    }
  } else if (kind == "conversation") {
    inspector.value.uncertain = !data.GetArray("items");
    inspector.Items(data.Find("items"));
  } else if (kind == "item") {
    inspector.value.uncertain =
        !data.Find("item") || !data.Find("item")->IsObject();
    inspector.Items(data.Find("item"));
  } else if (kind == "chat") {
    inspector.value.uncertain = !data.GetArray("messages");
    inspector.Items(data.Find("messages"));
  } else if (kind == "prompt") {
    inspector.value.uncertain = !data.GetArray("versions");
    if (const auto* versions = data.GetArray("versions"))
      for (const auto& version : versions->array) {
        if (const auto* definition = version.Find("definition");
            definition && definition->Find("request"))
          inspector.Request(definition->Find("request"));
        else
          inspector.value.uncertain = true;
      }
  } else if (kind == "batch" && data.GetString("format") == "q4t.batch.v1") {
    inspector.Blob(data);
    const auto* batch = data.Find("batch");
    const auto* pages = data.GetArray("pages");
    if (!batch || !batch->IsObject() || !pages ||
        pages->array.size() > kBatchPages) {
      inspector.value.uncertain = true;
    } else {
      const auto status = batch->GetString("status");
      inspector.value.active = status == "validating" ||
                               status == "in_progress" ||
                               status == "finalizing" || status == "cancelling";
      inspector.value.uncertain |= !inspector.value.active && !BatchTerminal(status);
      for (const auto& page : pages->array) inspector.Reference(&page);
      // Result Files have their own declared expiry. A historical Batch
      // receipt must not pin their content beyond output_expires_after.
    }
  } else if (kind == "file" && data.GetString("format") == "q4t.file.v1") {
    inspector.Blob(data);
  } else if (kind == "input_snapshot" &&
             data.GetString("format") == "q4t.input_snapshot.v1") {
    inspector.Blob(data);
    inspector.value.collect_when_unreferenced = true;
  } else if (kind == "tool_run") {
    const auto format = data.GetString("format");
    if (format == "q4t.binary_chunk.v1") {
      inspector.value.collect_when_unreferenced = true;
    } else if (format == "q4t.binary_stage.v1" ||
               format == "q4t.binary_read.v1") {
      inspector.Blob(data);
    } else if (format == "q4t.upload_part.v1") {
      inspector.Blob(data);
      inspector.value.collect_when_unreferenced = true;
    } else if (format == "q4t.batch_result.v1") {
      inspector.Blob(data);
      inspector.value.collect_when_unreferenced = true;
    } else if (format == "q4t.batch_page.v1") {
      inspector.value.collect_when_unreferenced = true;
      const auto* rows = data.GetArray("rows");
      if (!rows || rows->array.empty() || rows->array.size() > kBatchPageRows ||
          !ValidApplicationId(data.GetString("batch_id"))) {
        inspector.value.uncertain = true;
      } else {
        for (const auto& row : rows->array) {
          const auto status = row.GetString("status");
          inspector.value.uncertain |= status != "pending" && status != "running" &&
                                       status != "completed" && status != "failed";
          inspector.Reference(row.Find("result_id"));
          inspector.Snapshots(row);
          inspector.Request(row.Find("request_dependencies"));
        }
      }
    } else if (format == "q4t.input_snapshot_hold.v1") {
      const auto id = data.GetString("snapshot_id");
      inspector.value.uncertain = !id.starts_with("input_snapshot_");
      inspector.Reference(data.Find("snapshot_id"));
    } else if (format == "q4t.upload.v1") {
      const auto* upload = data.Find("upload");
      const auto* parts = data.GetArray("parts");
      if (!upload || !upload->IsObject() || !parts) {
        inspector.value.uncertain = true;
      } else if (upload->GetString("status") == "pending") {
        inspector.value.references_before = upload->GetInt("expires_at");
        inspector.value.uncertain = inspector.value.references_before <= 0;
        for (const auto& part : parts->array) inspector.Reference(&part);
      } else if (upload->GetString("status") != "completed" &&
                 upload->GetString("status") != "cancelled" &&
                 upload->GetString("status") != "expired") {
        inspector.value.uncertain = true;
      }
    } else {
      inspector.value.uncertain = true;
    }
  } else {
    // Future Files/Batch/tool executors must register their real lifecycle and
    // dependency extraction before automatic collection can safely use them.
    inspector.value.uncertain = true;
  }
  // Snapshot validation adds uncertainty; ordinary shape checks above must
  // never overwrite a malformed private dependency's conservative protection.
  if (kind == "response" || kind == "conversation" || kind == "item" ||
      kind == "chat" || kind == "prompt" || kind == "batch") {
    inspector.Snapshots(data);
    inspector.Payloads(data);
  }
  return inspector.Finish();
}

Status PlanObjectRetention(std::span<const ObjectRetentionNode> nodes,
                           int64_t now, int64_t item_created_before,
                           size_t limit, ObjectRetentionPlan* plan) {
  *plan = {};
  if (now <= 0 || item_created_before < 0 || item_created_before > now ||
      limit == 0 || limit > 128 || nodes.size() > 100000)
    return Status::Fail("Invalid retention planning limits");
  std::unordered_map<std::string, std::vector<size_t>> identities;
  std::unordered_set<std::string_view> uncertain_owners;
  std::vector<bool> candidate(nodes.size()), protected_node(nodes.size());
  std::deque<size_t> queue;
  std::string fingerprint =
      "q4t-retention-v1:" + std::to_string(item_created_before) + ':' +
      std::to_string(limit) + '\n';
  size_t bytes = nodes.size() * (sizeof(ObjectRetentionNode) + 128);
  for (size_t i = 0; i < nodes.size(); ++i) {
    const auto& n = nodes[i];
    if (!n.retention || n.owner.size() != 64 || n.digest.size() != 64 ||
        !ValidApplicationId(n.id) || n.kind.empty() || n.created <= 0 ||
        n.expires < 0)
      return Status::Fail("Invalid retention metadata");
    bytes += n.retention->Bytes() + n.owner.size() + n.id.size() +
             n.kind.size() + n.digest.size();
    if (bytes > kPlanBytes)
      return Status::Fail("Retention planning metadata limit exceeded");
    identities[Key(n.owner, n.id)].push_back(i);
    if (n.retention->uncertain) uncertain_owners.insert(n.owner);
    candidate[i] = n.retention->collect_when_unreferenced ||
                   (n.expires > 0 && n.expires <= now) ||
                   (item_created_before && n.kind == "item" && n.expires == 0 &&
                    n.created < item_created_before);
    fingerprint += std::string(n.owner) + ':' + std::string(n.kind) + ':' +
                   std::string(n.id) + ':' + std::string(n.digest) + '\n';
  }
  for (size_t i = 0; i < nodes.size(); ++i) {
    const auto& n = nodes[i];
    const bool unknown = uncertain_owners.contains(n.owner);
    plan->active += n.retention->active;
    plan->uncertain += unknown;
    if (!candidate[i] || n.retention->active || unknown) {
      protected_node[i] = true;
      queue.push_back(i);
    }
  }
  size_t work = 0;
  while (!queue.empty()) {
    const auto i = queue.front();
    queue.pop_front();
    if (nodes[i].retention->references_before > 0 &&
        nodes[i].retention->references_before <= now)
      continue;
    for (const auto& ref : nodes[i].retention->references) {
      if (++work > 2 * 1024 * 1024)
        return Status::Fail("Retention reference work limit exceeded");
      const auto found = identities.find(Key(nodes[i].owner, ref));
      if (found == identities.end()) continue;
      for (const auto target : found->second)
        if (!protected_node[target]) {
          protected_node[target] = true;
          queue.push_back(target);
        }
    }
  }
  std::string_view owner;
  for (size_t i = 0; i < nodes.size(); ++i) {
    if (!candidate[i]) continue;
    if (protected_node[i]) {
      plan->referenced += !nodes[i].retention->active &&
                          !uncertain_owners.contains(nodes[i].owner);
      continue;
    }
    ++plan->eligible;
    if (owner.empty()) owner = nodes[i].owner;
    if (plan->selected.size() < limit && nodes[i].owner == owner) {
      plan->selected.push_back(i);
      plan->bytes += nodes[i].bytes;
      fingerprint += "delete:" + std::to_string(i) + '\n';
    }
  }
  fingerprint += "eligible:" + std::to_string(plan->eligible);
  plan->sha256 = trace::HexDigest(
      trace::Sha256({reinterpret_cast<const uint8_t*>(fingerprint.data()),
                     fingerprint.size()}));
  return {};
}
}  // namespace q4t::server
