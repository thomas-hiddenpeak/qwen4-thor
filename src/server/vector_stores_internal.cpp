#include "vector_stores_internal.h"

#include <algorithm>
#include <ctime>
#include <limits>
#include <unordered_set>

#include "q4t/server/files.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"

namespace q4t::server::vector_store_detail {
namespace {
bool Text(const Json& value, size_t maximum) {
  return value.IsString() && detail::NormalizeUtf8(value.str) == value.str &&
         static_cast<size_t>(std::count_if(
             value.str.begin(), value.str.end(),
             [](unsigned char c) { return (c & 0xc0) != 0x80; })) <= maximum;
}
bool State(std::string_view value) {
  return value == "in_progress" || value == "completed" || value == "failed" ||
         value == "cancelled";
}
ObjectReply Metadata(const Json& value) {
  if (value.IsNull()) return {};
  if (!value.IsObject() || value.object.size() > 16)
    return Invalid("metadata requires at most 16 string entries", "metadata");
  std::unordered_set<std::string> keys;
  for (const auto& [key, item] : value.object)
    if (!Text(String(key), 64) || !Text(item, 512) || !keys.insert(key).second)
      return Invalid("Invalid metadata key or value",
                     InputParamPath("metadata", key));
  return {};
}
ObjectReply Policy(const Json& value) {
  if (value.IsNull()) return {};
  if (!InputFields(value, {"anchor", "days"}) ||
      value.GetString("anchor") != "last_active_at" || !value.Find("days") ||
      !IsInputInteger(*value.Find("days")) || value.GetInt("days") < 1 ||
      value.GetInt("days") > 365)
    return Invalid("expires_after requires last_active_at and 1..365 days",
                   "expires_after");
  return {};
}
}  // namespace

ObjectReply Invalid(std::string message, std::string param) {
  auto reply = Error(400, "invalid_request", std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}
ObjectReply Corrupt() {
  return Error(503, "vector_store_state_invalid",
               "Vector store state is inconsistent");
}
ObjectReply Expired() {
  return Error(409, "vector_store_expired", "Vector store has expired");
}
ObjectReply Result(Json value) {
  ObjectReply reply;
  reply.value = std::move(value);
  return reply;
}
bool Retry(const ObjectReply& reply) {
  return !reply.commit_unknown && reply.code == "object_revision_conflict";
}
bool Live(const Json& data) {
  const auto* store = data.Find("vector_store");
  return store && (store->GetInt("expires_at") == 0 ||
                   store->GetInt("expires_at") > std::time(nullptr));
}
ObjectRequest Write(const BinaryAccess& access, ObjectKind kind,
                    const std::string& id, int64_t revision, Json data,
                    int64_t mutate_before) {
  ObjectRequest request;
  request.operation = ObjectOperation::kPut;
  request.kind = kind;
  request.scope = access.scope;
  request.id = id;
  request.expected_revision = revision;
  request.ttl_seconds = 0;
  request.mutate_before = mutate_before;
  request.data = std::move(data);
  return request;
}
const Json* Edge(const Json& data, const std::string& file_id) {
  const auto* members = data.GetArray("members");
  if (!members) return nullptr;
  for (const auto& edge : members->array)
    if (edge.GetString("file_id") == file_id) return &edge;
  return nullptr;
}
void Summarize(Json* data) {
  auto& store = Field(*data, "vector_store");
  auto counts = Object({{"in_progress", Number(0)},
                        {"completed", Number(0)},
                        {"failed", Number(0)},
                        {"cancelled", Number(0)},
                        {"total", Number(0)}});
  int64_t bytes = 0;
  for (const auto& edge : Field(*data, "members").array) {
    auto& count = Field(counts, edge.GetString("status"));
    count = Number(count.AsInt() + 1);
    bytes += edge.GetInt("usage_bytes");
  }
  Set(&counts, "total", Number(Field(*data, "members").array.size()));
  Set(&store, "status",
      String(counts.GetInt("in_progress") > 0 ? "in_progress" : "completed"));
  Set(&store, "file_counts", std::move(counts));
  Set(&store, "usage_bytes", Number(bytes));
}
void Activity(Json* data) {
  auto& store = Field(*data, "vector_store");
  // Wall-clock regressions must not shorten a published activity window.
  const auto now =
      std::max<int64_t>(store.GetInt("last_active_at"), std::time(nullptr));
  Set(&store, "last_active_at", Number(now));
  const auto* policy = store.Find("expires_after");
  Set(&store, "expires_at",
      policy && !policy->IsNull() ? Number(now + policy->GetInt("days") * 86400)
                                  : Json());
}
Json PublicStore(const Json& data) {
  auto result = *data.Find("vector_store");
  if (!Live(data)) {
    Set(&result, "status", String("expired"));
    Set(&result, "usage_bytes", Number(0));
    auto counts = *result.Find("file_counts");
    for (auto& [key, value] : counts.object) value = Number(0);
    Set(&result, "file_counts", std::move(counts));
  }
  return result;
}
ObjectReply Fields(const Json& request, bool creating) {
  std::string param;
  auto valid =
      creating
          ? InputFields(request,
                        {"name", "description", "metadata", "expires_after",
                         "file_ids", "chunking_strategy"},
                        &param)
          : InputFields(request, {"name", "metadata", "expires_after"}, &param);
  if (!valid) return Invalid(valid.message(), param);
  for (const auto* key : {"name", "description"})
    if (const auto* value = request.Find(key);
        value &&
        !(value->IsNull() && !creating && std::string_view(key) == "name") &&
        !Text(*value, 4096))
      return Invalid("Expected UTF-8 string of at most 4096 characters", key);
  if (const auto* value = request.Find("metadata")) {
    auto result = Metadata(*value);
    if (!result.ok()) return result;
  }
  if (const auto* value = request.Find("expires_after")) {
    auto result = Policy(*value);
    if (!result.ok()) return result;
  }
  ChatPlanError error;
  VectorChunking chunking;
  if (const auto* value = request.Find("chunking_strategy");
      value && !ParseVectorChunking(*value, &chunking, &error))
    return Invalid("Invalid chunking strategy", error.param);
  if (const auto* ids = request.Find("file_ids")) {
    if (!ids->IsArray() || ids->array.size() > 500)
      return Invalid("file_ids requires an array of at most 500 IDs",
                     "file_ids");
    std::unordered_set<std::string> seen;
    for (const auto& id : ids->array)
      if (!id.IsString() || !ValidId(id.str) || !id.str.starts_with("file_") ||
          !seen.insert(id.str).second)
        return Invalid("Invalid or duplicate file ID", "file_ids");
  }
  return {};
}
int64_t SearchRevision(const Json& record) {
  const auto* data = record.Find("data");
  return data ? data->GetInt("search_revision", record.GetInt("revision"))
              : record.GetInt("revision");
}
bool ValidateStore(const Json& record) {
  const auto* data = record.Find("data");
  const auto* store = data ? data->Find("vector_store") : nullptr;
  const auto* members = data ? data->GetArray("members") : nullptr;
  if (data && data->Has("search_revision")) {
    const auto& revision = *data->Find("search_revision");
    if (!IsInputInteger(revision) || revision.AsInt() < 1 ||
        revision.AsInt() > 9007199254740991LL)
      return false;
  }
  if (!data || data->GetString("format") != kStore || !store ||
      !store->IsObject() || !members ||
      members->array.size() > kVectorStoreFiles ||
      store->GetString("id") != record.GetString("id") ||
      !store->GetString("id").starts_with("vs_") ||
      store->GetString("object") != "vector_store" ||
      store->GetInt("created_at") != record.GetInt("created_at") ||
      record.GetInt("created_at") <= 0 ||
      record.GetInt("expires_at", -1) != 0 ||
      store->GetInt("last_active_at") < record.GetInt("created_at") ||
      store->GetInt("last_active_at") >
          std::numeric_limits<int64_t>::max() - 365 * 86400 ||
      !store->Find("metadata") || !Metadata(*store->Find("metadata")).ok() ||
      !store->Find("expires_after") ||
      !Policy(*store->Find("expires_after")).ok())
    return false;
  const auto days = store->Find("expires_after")->GetInt("days");
  if (store->GetInt("expires_at") !=
      (days ? store->GetInt("last_active_at") + days * 86400 : 0))
    return false;
  std::unordered_set<std::string> ids, children;
  for (const auto& edge : members->array)
    if (!ValidId(edge.GetString("file_id")) ||
        !edge.GetString("file_id").starts_with("file_") ||
        !ValidId(edge.GetString("id")) ||
        !edge.GetString("id").starts_with("vector_store_file_") ||
        !ids.insert(edge.GetString("file_id")).second ||
        !children.insert(edge.GetString("id")).second ||
        !State(edge.GetString("status")) || edge.GetInt("created_at") <= 0 ||
        edge.GetInt("usage_bytes", -1) < 0 ||
        edge.GetInt("usage_bytes") > (64LL << 20) ||
        (edge.GetString("status") != "completed" &&
         edge.GetInt("usage_bytes") != 0))
      return false;
  std::unordered_set<std::string> batches;
  if (const auto* refs = data->GetArray("batches")) {
    if (refs->array.size() > kVectorStoreFiles) return false;
    for (const auto& ref : refs->array)
      if (!ref.IsString() || !ValidId(ref.str) || !ref.str.starts_with("vsfb_") ||
          !batches.insert(ref.str).second)
        return false;
  } else if (data->Has("batches")) return false;
  for (const auto& edge : members->array)
    if (edge.Has("batch_id") &&
        (!edge.Find("batch_id")->IsString() ||
         !batches.contains(edge.GetString("batch_id"))))
      return false;
  auto expected = *data;
  Summarize(&expected);
  const auto* projection = expected.Find("vector_store");
  for (const auto* key : {"file_counts", "usage_bytes", "status"})
    if (!store->Find(key) ||
        OutputJson(*store->Find(key)) != OutputJson(*projection->Find(key)))
      return false;
  return true;
}
bool ValidateMember(const Json& record, const Json& store) {
  const auto* data = record.Find("data");
  const auto* file = data ? data->Find("file") : nullptr;
  const auto* job = data ? data->Find("job") : nullptr;
  const auto* edge =
      file ? Edge(*store.Find("data"), file->GetString("id")) : nullptr;
  if (!data || data->GetString("format") != kMember || !file || !job || !edge ||
      edge->GetString("id") != record.GetString("id") ||
      file->GetString("vector_store_id") != store.GetString("id") ||
      file->GetString("object") != "vector_store.file" ||
      file->GetInt("created_at") != record.GetInt("created_at") ||
      file->GetInt("created_at") != edge->GetInt("created_at") ||
      file->GetString("status") != edge->GetString("status") ||
      file->GetInt("usage_bytes", -1) != edge->GetInt("usage_bytes") ||
      record.GetInt("expires_at", -1) != 0 ||
      data->GetString("scope").empty() || !Text(*file->Find("id"), 200))
    return false;
  if (data->GetString("batch_id") != edge->GetString("batch_id")) return false;
  if (data->Has("detached") && !data->Find("detached")->IsBool()) return false;
  ChatPlanError error;
  VectorChunking chunking;
  if (!file->Find("attributes") ||
      !ValidateVectorAttributes(*file->Find("attributes"), &error) ||
      !file->Find("chunking_strategy") ||
      !ParseVectorChunking(*file->Find("chunking_strategy"), &chunking, &error))
    return false;
  const auto state = job->GetString("state");
  if (job->Has("cancel_requested") &&
      !job->Find("cancel_requested")->IsBool())
    return false;
  if (job->GetInt("attempts", -1) < 0 || job->GetInt("attempts") > 3 ||
      job->GetInt("execution_deadline_at") <= record.GetInt("created_at"))
    return false;
  if (file->GetString("status") == "in_progress") {
    if (state != "queued" && state != "running") return false;
    if (state == "running" &&
        (job->GetString("token").empty() || job->GetString("owner").empty() ||
         job->GetInt("deadline_at") <= 0))
      return false;
  } else if (state != "terminal") {
    return false;
  }
  const auto* refs = data->GetArray("input_snapshots");
  if (!refs || refs->array.size() !=
                   (file->GetString("status") == "completed" ? 1U : 0U))
    return false;
  if (file->GetString("status") == "completed") {
    VectorIndexIdentity identity;
    InputSnapshotRef reference;
    const auto& binding = refs->array.front();
    if (!data->Find("index_identity") ||
        !ParseVectorIndexIdentity(*data->Find("index_identity"), &identity) ||
        identity.file_id != file->GetString("id") ||
        identity.filename != data->GetString("filename") ||
        identity.chunking.max_tokens != chunking.max_tokens ||
        identity.chunking.overlap_tokens != chunking.overlap_tokens ||
        !binding.Find("snapshot") ||
        !ParseInputSnapshotReference(*binding.Find("snapshot"), &reference) ||
        binding.GetString("binding_sha256") != VectorIndexBinding(identity))
      return false;
  }
  return true;
}
ObjectReply PrepareMember(ObjectStore& objects, const BinaryAccess& access,
                          const std::string& id, const Json& request,
                          int64_t created_at, Json* data, Json* edge) {
  std::string param;
  auto valid = InputFields(
      request, {"file_id", "attributes", "chunking_strategy"}, &param);
  if (!valid) return Invalid(valid.message(), param);
  const auto file_id = request.GetString("file_id");
  if (!ValidId(file_id) || !file_id.starts_with("file_"))
    return Invalid("Expected a Files ID", "file_id");
  ChatPlanError error;
  Json attributes = Object();
  if (const auto* value = request.Find("attributes")) {
    if (!ValidateVectorAttributes(*value, &error))
      return Invalid("Invalid file attributes", error.param);
    if (!value->IsNull()) attributes = *value;
  }
  VectorChunking chunking;
  if (const auto* value = request.Find("chunking_strategy");
      value && !ParseVectorChunking(*value, &chunking, &error))
    return Invalid("Invalid chunking strategy", error.param);
  auto file = Files(objects).Retrieve(access, file_id);
  if (!file.ok()) return file;
  const auto member_id = NewObjectId(ObjectKind::kVectorStoreFile);
  if (member_id.empty())
    return Error(503, "object_id_unavailable", "No member ID");
  auto public_file = Object(
      {{"id", String(file_id)},
       {"object", String("vector_store.file")},
       {"created_at", Number(created_at)},
       {"vector_store_id", String(id)},
       {"status", String("in_progress")},
       {"usage_bytes", Number(0)},
       {"last_error", Json()},
       {"attributes", std::move(attributes)},
       {"chunking_strategy",
        Object({{"type", String("static")},
                {"static",
                 Object({{"max_chunk_size_tokens", Number(chunking.max_tokens)},
                         {"chunk_overlap_tokens",
                          Number(chunking.overlap_tokens)}})}})}});
  *data = Object({{"format", String(std::string(kMember))},
                  {"scope", String(access.scope)},
                  {"filename", String(file.value.GetString("filename"))},
                  {"file", std::move(public_file)},
                  {"input_snapshots", Array()},
                  {"job", Object({{"state", String("queued")},
                                  {"attempts", Number(0)},
                                  {"execution_deadline_at",
                                   Number(created_at + 86400)}})}});
  *edge = Object({{"id", String(member_id)},
                  {"file_id", String(file_id)},
                  {"created_at", Number(created_at)},
                  {"status", String("in_progress")},
                  {"usage_bytes", Number(0)}});
  return {};
}
ObjectReply MatchClaim(const Json& data, const VectorIngestionClaim& claim,
                       bool require_live) {
  const auto* file = data.Find("file");
  const auto* job = data.Find("job");
  if (!file || !job || file->GetString("id") != claim.file_id ||
      file->GetString("vector_store_id") != claim.store_id ||
      file->GetString("status") != "in_progress" ||
      job->GetString("state") != "running" || claim.token.empty() ||
      job->GetString("token") != claim.token ||
      job->GetString("owner") != claim.owner ||
      job->GetInt("deadline_at") != claim.deadline_at)
    return Error(409, "vector_ingestion_claim_conflict",
                 "Ingestion owner changed");
  if (require_live && claim.deadline_at <= std::time(nullptr))
    return Error(408, "vector_ingestion_deadline",
                 "Ingestion deadline exceeded");
  if (require_live && job->GetBool("cancel_requested"))
    return Error(499, "vector_ingestion_cancelled", "Ingestion cancelled");
  return {};
}
}  // namespace q4t::server::vector_store_detail
