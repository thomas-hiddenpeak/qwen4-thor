#include "q4t/server/vector_stores.h"

#include <algorithm>
#include <ctime>
#include <map>
#include <set>

#include "q4t/server/input_contract.h"
#include "vector_stores_internal.h"

namespace q4t::server {
using namespace vector_store_detail;
namespace {
constexpr size_t kBatchFiles = 2000;
constexpr std::string_view kFormat = "q4t.vector_store_batch.v1";
bool Referenced(const Json& data, const std::string& id) {
  const auto* batches = data.GetArray("batches");
  return batches && std::any_of(batches->array.begin(), batches->array.end(),
                                [&](const auto& v) { return v.str == id; });
}
void SummarizeBatch(Json* data) {
  auto counts = Object({{"in_progress", Number(0)},
                        {"completed", Number(0)},
                        {"failed", Number(0)},
                        {"cancelled", Number(0)},
                        {"total", Number(0)}});
  for (const auto& member : data->GetArray("members")->array) {
    const auto state = member.GetString("status");
    Set(&counts, state, Number(counts.GetInt(state) + 1));
    Set(&counts, "total", Number(counts.GetInt("total") + 1));
  }
  const auto* status =
      counts.GetInt("in_progress") ? "in_progress"
      : data->GetBool("cancel_requested") || counts.GetInt("cancelled")
          ? "cancelled"
      : counts.GetInt("failed") ? "failed"
                                : "completed";
  auto& batch = Field(*data, "batch");
  Set(&batch, "status", String(status));
  Set(&batch, "file_counts", std::move(counts));
}
bool ValidateBatch(const Json& record, const std::string& store,
                   const std::string& scope) {
  const auto* data = record.Find("data");
  const auto* batch = data ? data->Find("batch") : nullptr;
  const auto* members = data ? data->GetArray("members") : nullptr;
  if (!data || data->GetString("format") != kFormat ||
      data->GetString("scope") != scope || !batch || !members ||
      members->array.empty() || members->array.size() > kBatchFiles ||
      batch->GetString("id") != record.GetString("id") ||
      batch->GetString("vector_store_id") != store ||
      batch->GetString("object") != "vector_store.files_batch" ||
      batch->GetInt("created_at") != record.GetInt("created_at") ||
      !data->Find("cancel_requested") ||
      !data->Find("cancel_requested")->IsBool() ||
      record.GetInt("expires_at", -1) != 0)
    return false;
  std::set<std::string> files, ids;
  for (const auto& row : members->array) {
    const auto* file = row.Find("file");
    const auto status = row.GetString("status");
    ChatPlanError error;
    if (!file || !ValidId(row.GetString("id")) ||
        !ids.insert(row.GetString("id")).second ||
        !files.insert(row.GetString("file_id")).second ||
        file->GetString("id") != row.GetString("file_id") ||
        file->GetString("vector_store_id") != store ||
        file->GetString("object") != "vector_store.file" ||
        file->GetString("status") != status ||
        file->GetInt("created_at") != row.GetInt("created_at") ||
        file->GetInt("usage_bytes", -1) != row.GetInt("usage_bytes") ||
        !file->Find("attributes") ||
        !ValidateVectorAttributes(*file->Find("attributes"), &error) ||
        (status != "in_progress" && status != "completed" &&
         status != "failed" && status != "cancelled") ||
        (row.Has("detached") && !row.Find("detached")->IsBool()))
      return false;
  }
  auto expected = *data;
  SummarizeBatch(&expected);
  return OutputJson(*expected.Find("batch")) == OutputJson(*batch);
}
}  // namespace

ObjectReply VectorStores::ReadBatch(const BinaryAccess& access,
                                    const Json& store,
                                    const std::string& batch_id) {
  if (!ValidId(batch_id) || !batch_id.starts_with("vsfb_"))
    return Invalid("Invalid vector file batch ID", "batch_id");
  if (!Referenced(*store.Find("data"), batch_id))
    return Error(404, "vector_batch_not_found", "Batch is not in this store");
  auto query = Write(access, ObjectKind::kVectorStoreBatch, batch_id, 0, {});
  query.operation = ObjectOperation::kGet;
  auto reply = objects_.Execute(query, access.deadline, access.cancelled);
  if (!reply.ok()) return reply;
  if (!ValidateBatch(reply.value, store.GetString("id"), access.scope))
    return Corrupt();
  return reply;
}

ObjectReply VectorStores::ReadExecutionMember(const BinaryAccess& access,
                                              const Json& store,
                                              const std::string& file_id,
                                              const std::string& member_id) {
  const auto* edge = Edge(*store.Find("data"), file_id);
  if (edge && edge->GetString("id") == member_id)
    return ReadMember(access, store, file_id);
  if (!ValidId(member_id) || !member_id.starts_with("vector_store_file_"))
    return Invalid("Invalid private execution identity");
  auto query = Write(access, ObjectKind::kVectorStoreFile, member_id, 0, {});
  query.operation = ObjectOperation::kGet;
  auto reply = objects_.Execute(query, access.deadline, access.cancelled);
  if (!reply.ok()) {
    if (edge && reply.http_status == 404)
      return Error(409, "vector_ingestion_claim_conflict",
                   "File membership changed");
    return reply;
  }
  const auto* data = reply.value.Find("data");
  const auto* file = data ? data->Find("file") : nullptr;
  if (!data || !file || !data->GetBool("detached") ||
      data->GetString("scope") != access.scope ||
      file->GetString("id") != file_id ||
      file->GetString("vector_store_id") != store.GetString("id"))
    return edge ? Error(409, "vector_ingestion_claim_conflict",
                        "File membership changed")
                : Error(404, "vector_execution_not_found",
                        "Execution no longer retained");
  auto batch = ReadBatch(access, store, data->GetString("batch_id"));
  if (!batch.ok()) return batch;
  const auto& rows = batch.value.Find("data")->GetArray("members")->array;
  const auto found = std::find_if(rows.begin(), rows.end(), [&](const auto& r) {
    return r.GetString("id") == member_id &&
           r.GetString("file_id") == file_id && r.GetBool("detached");
  });
  if (found == rows.end()) return Corrupt();
  auto synthetic = store;
  auto& edges = Field(Field(synthetic, "data"), "members").array;
  std::erase_if(
      edges, [&](const auto& r) { return r.GetString("file_id") == file_id; });
  edges.push_back(*found);
  if (!ValidateMember(reply.value, synthetic)) {
    auto fresh = Read(access, store.GetString("id"), true);
    if (!fresh.ok()) return fresh;
    if (fresh.value.GetInt("revision") != store.GetInt("revision"))
      return Error(409, "object_revision_conflict", "Vector store changed");
    return Corrupt();
  }
  return reply;
}
ObjectReply VectorStores::LoadExecution(const BinaryAccess& access,
                                        const std::string& id,
                                        const std::string& file_id,
                                        const std::string& member_id) {
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto root = Read(access, id, true);
    if (!root.ok()) return root;
    auto reply = ReadExecutionMember(access, root.value, file_id, member_id);
    if (!Retry(reply)) return reply;
  }
  return Error(409, "object_revision_conflict", "Vector store kept changing");
}

ObjectReply VectorStores::SyncBatches(const BinaryAccess& access,
                                      const Json& before, const Json& data,
                                      std::span<const ObjectRequest> children,
                                      std::vector<ObjectRequest>* mutations) {
  // Gather small receipt mutations separately: appending to mutations may
  // invalidate data, which aliases the parent request in that same vector.
  std::set<std::string> affected, provided;
  for (const auto& child : children) {
    if (child.kind == ObjectKind::kVectorStoreBatch) provided.insert(child.id);
    if (child.kind == ObjectKind::kVectorStoreFile) {
      const auto id = child.data.GetString("batch_id");
      if (!id.empty()) affected.insert(id);
    }
  }
  const auto* old = before.Find("data");
  if (old)
    for (const auto& edge : old->GetArray("members")->array) {
      const auto id = edge.GetString("batch_id");
      const auto* after = Edge(data, edge.GetString("file_id"));
      if (!id.empty() &&
          (!after || after->GetString("id") != edge.GetString("id")))
        affected.insert(id);
    }
  std::vector<ObjectRequest> changes;
  for (const auto& id : affected) {
    if (provided.contains(id)) continue;
    auto loaded = ReadBatch(access, before, id);
    if (!loaded.ok()) return loaded;
    auto batch_data = *loaded.value.Find("data");
    for (auto& row : Field(batch_data, "members").array) {
      bool changed = false;
      for (const auto& child : children)
        if (child.kind == ObjectKind::kVectorStoreFile &&
            child.id == row.GetString("id")) {
          const auto& file = *child.data.Find("file");
          Set(&row, "file", file);
          Set(&row, "status", *file.Find("status"));
          Set(&row, "usage_bytes", *file.Find("usage_bytes"));
          changed = true;
          break;
        }
      const auto* edge = Edge(data, row.GetString("file_id"));
      if (!edge || edge->GetString("id") != row.GetString("id")) {
        // A detached running member stays referenced until its original
        // executor drains (or its process is proven gone). Deletion hides
        // retrieval immediately without pretending the work has stopped.
        if (!row.GetBool("detached") && !changed &&
            row.GetString("status") == "in_progress")
          return Corrupt();
        Set(&row, "detached", Boolean(true));
      }
    }
    SummarizeBatch(&batch_data);
    changes.push_back(Write(access, ObjectKind::kVectorStoreBatch, id,
                            loaded.value.GetInt("revision"),
                            std::move(batch_data)));
  }
  mutations->insert(mutations->end(), changes.begin(), changes.end());
  return {};
}

ObjectReply VectorStores::CreateBatch(const BinaryAccess& access,
                                      const std::string& id,
                                      const Json& request) {
  std::string param;
  auto valid = InputFields(
      request, {"file_ids", "files", "attributes", "chunking_strategy"},
      &param);
  if (!valid) return Invalid(valid.message(), param);
  if (request.Has("file_ids") == request.Has("files"))
    return Invalid("Specify exactly one of file_ids or files", "file_ids");
  const bool per_file = request.Has("files");
  const auto field = per_file ? "files" : "file_ids";
  const auto* input = request.GetArray(field);
  if (!input || input->array.empty() || input->array.size() > kBatchFiles)
    return Invalid("Expected between 1 and 2000 files", field);
  const auto batch_id = NewObjectId(ObjectKind::kVectorStoreBatch);
  if (batch_id.empty())
    return Error(503, "object_id_unavailable", "No batch ID");
  auto initial = Read(access, id, true);
  if (!initial.ok()) return initial;
  const auto now = std::time(nullptr);
  auto batch_data =
      Object({{"format", String(std::string(kFormat))},
              {"scope", String(access.scope)},
              {"cancel_requested", Boolean(false)},
              {"members", Array()},
              {"batch", Object({{"id", String(batch_id)},
                                {"object", String("vector_store.files_batch")},
                                {"vector_store_id", String(id)},
                                {"created_at", Number(now)}})}});
  std::vector<ObjectRequest> writes;
  std::vector<Json> edges;
  std::set<std::string> files;
  for (const auto& value : input->array) {
    auto entry = per_file ? value : Object({{"file_id", value}});
    if (!per_file)
      for (const auto* key : {"attributes", "chunking_strategy"})
        if (const auto* v = request.Find(key)) Set(&entry, key, *v);
    Json child, edge;
    auto reply = PrepareMember(objects_, access, id, entry, now, &child, &edge);
    if (!reply.ok()) return reply;
    if (!files.insert(edge.GetString("file_id")).second)
      return Invalid("Duplicate file in batch", field);
    Set(&child, "batch_id", String(batch_id));
    Set(&edge, "batch_id", String(batch_id));
    auto receipt = edge;
    Set(&receipt, "file", *child.Find("file"));
    Field(batch_data, "members").array.push_back(std::move(receipt));
    auto write = Write(access, ObjectKind::kVectorStoreFile,
                       edge.GetString("id"), 0, std::move(child));
    write.created_at = now;
    writes.push_back(std::move(write));
    edges.push_back(std::move(edge));
  }
  SummarizeBatch(&batch_data);
  auto write =
      Write(access, ObjectKind::kVectorStoreBatch, batch_id, 0, batch_data);
  write.created_at = now;
  writes.push_back(std::move(write));
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto root = attempt ? Read(access, id, true) : std::move(initial);
    if (!root.ok()) return root;
    auto data = *root.value.Find("data");
    auto& members = Field(data, "members");
    if (members.array.size() + edges.size() > kVectorStoreFiles)
      return Error(409, "vector_store_capacity",
                   "Vector store file limit reached");
    for (const auto& edge : edges) {
      if (Edge(data, edge.GetString("file_id")))
        return Error(409, "vector_store_file_exists",
                     "File is already attached");
      members.array.push_back(edge);
    }
    if (!data.Has("batches")) Set(&data, "batches", Array());
    auto& batches = Field(data, "batches");
    if (batches.array.size() >= kVectorStoreFiles)
      return Error(409, "vector_store_capacity",
                   "Batch receipt capacity reached");
    batches.array.push_back(String(batch_id));
    auto reply = Publish(access, root.value, std::move(data), writes);
    if (Retry(reply)) continue;
    if (reply.ok()) reply.value = *batch_data.Find("batch");
    if (reply.commit_unknown)
      reply.value =
          Object({{"id", String(batch_id)}, {"vector_store_id", String(id)}});
    return reply;
  }
  return Error(409, "object_revision_conflict", "Vector store kept changing");
}
ObjectReply VectorStores::RetrieveBatch(const BinaryAccess& access,
                                        const std::string& id,
                                        const std::string& batch_id) {
  auto root = Read(access, id, true);
  if (!root.ok()) return root;
  auto reply = ReadBatch(access, root.value, batch_id);
  if (reply.ok()) reply.value = *reply.value.Find("data")->Find("batch");
  return reply;
}
ObjectReply VectorStores::CancelBatch(const BinaryAccess& access,
                                      const std::string& id,
                                      const std::string& batch_id) {
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto root = Read(access, id, true);
    if (!root.ok()) return root;
    auto batch = ReadBatch(access, root.value, batch_id);
    if (!batch.ok()) return batch;
    auto data = *batch.value.Find("data");
    if (data.Find("batch")->GetString("status") != "in_progress" ||
        data.GetBool("cancel_requested"))
      return Result(*data.Find("batch"));
    std::vector<ObjectRequest> writes;
    bool retry = false;
    for (const auto& row : data.GetArray("members")->array) {
      if (row.GetString("status") != "in_progress") continue;
      auto member = ReadExecutionMember(
          access, root.value, row.GetString("file_id"), row.GetString("id"));
      if (Retry(member)) {
        retry = true;
        break;
      }
      if (!member.ok()) return member;
      if (member.value.GetString("id") != row.GetString("id")) return Corrupt();
      auto child = *member.value.Find("data");
      Set(&Field(child, "job"), "cancel_requested", Boolean(true));
      writes.push_back(
          Write(access, ObjectKind::kVectorStoreFile, row.GetString("id"),
                member.value.GetInt("revision"), std::move(child)));
    }
    if (retry) continue;
    Set(&data, "cancel_requested", Boolean(true));
    writes.push_back(Write(access, ObjectKind::kVectorStoreBatch, batch_id,
                           batch.value.GetInt("revision"), data));
    auto reply = Publish(access, root.value, *root.value.Find("data"), writes);
    if (Retry(reply)) continue;
    if (reply.ok()) reply.value = *data.Find("batch");
    if (reply.commit_unknown)
      reply.value =
          Object({{"id", String(batch_id)}, {"vector_store_id", String(id)}});
    return reply;
  }
  return Error(409, "object_revision_conflict", "Vector store kept changing");
}
ObjectReply VectorStores::ListBatchFiles(const BinaryAccess& access,
                                         const std::string& id,
                                         const std::string& batch_id,
                                         const Json& page) {
  auto root = Read(access, id, true);
  if (!root.ok()) return root;
  auto batch = ReadBatch(access, root.value, batch_id);
  if (!batch.ok()) return batch;
  std::vector<Json> rows;
  bool more = false;
  auto reply = SelectFilePage(
      batch.value.Find("data")->GetArray("members")->array, page, &rows, &more);
  if (!reply.ok()) return reply;
  auto items = Array();
  for (auto& row : rows) items.array.push_back(*row.Find("file"));
  return FilePage(std::move(items), more);
}
}  // namespace q4t::server
