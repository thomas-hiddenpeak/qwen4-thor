#include "q4t/server/batch_state.h"

#include <algorithm>
#include <array>
#include <ctime>
#include <limits>

#include "object_protocol.h"
#include "q4t/server/files.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/responses_owner.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
ObjectReply Invalid(std::string message, std::string param = "batch_id") {
  auto reply = Error(400, "invalid_request", std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}
ObjectReply Corrupt() {
  return Error(503, "batch_state_invalid", "Stored batch state is invalid");
}
bool Hash(std::string_view value) {
  return value.size() == 64 &&
         std::all_of(value.begin(), value.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}
ObjectRequest Request(const BinaryAccess& access, const std::string& id,
                      ObjectOperation operation) {
  ObjectRequest request;
  request.operation = operation;
  request.kind = ObjectKind::kBatch;
  request.scope = access.scope;
  request.id = id;
  return request;
}
ObjectRequest Update(const BinaryAccess& access, const Json& stored) {
  auto request = Request(access, stored.GetString("id"), ObjectOperation::kPut);
  request.data = *stored.Find("data");
  request.expected_revision = stored.GetInt("revision");
  request.created_at = stored.GetInt("created_at");
  request.expires_at = stored.GetInt("expires_at");
  return request;
}
void Identify(ObjectReply* reply, const std::string& id) {
  if (!reply->value.IsObject()) reply->value = Object();
  Set(&reply->value, "id", String(id));
}
ObjectReply Owner(Json* owner) {
  try {
    *owner = CurrentResponseOwner();
    return {};
  } catch (...) {
    return Error(503, "batch_owner_unavailable",
                 "Batch process identity unavailable");
  }
}
bool Integer(const Json& object, const std::string& key, int64_t minimum,
             int64_t maximum) {
  const auto* value = object.Find(key);
  return value && IsInputInteger(*value) && value->AsInt() >= minimum &&
         value->AsInt() <= maximum;
}
ObjectReply Public(const Json& stored) {
  const auto* data = stored.Find("data");
  const auto* batch = data ? data->Find("batch") : nullptr;
  const auto* blob = data ? data->Find("blob") : nullptr;
  const auto* pages = data ? data->GetArray("pages") : nullptr;
  BinaryManifest manifest;
  if (!Integer(stored, "created_at", 1,
               std::numeric_limits<int64_t>::max() - kBatchWindowSeconds) ||
      !data || data->GetString("format") != "q4t.batch.v1" || !batch ||
      !batch->IsObject() || batch->GetString("object") != "batch" ||
      batch->GetString("id") != stored.GetString("id") || !pages ||
      pages->array.size() > kBatchPages || !blob ||
      !ParseBinaryManifest(*blob, &manifest) ||
      manifest.bytes > kBatchInputBytes || !manifest.bytes ||
      !Integer(*batch, "created_at", 1, std::numeric_limits<int64_t>::max()) ||
      !Integer(*batch, "expires_at", 1, std::numeric_limits<int64_t>::max()) ||
      batch->GetInt("created_at") != stored.GetInt("created_at") ||
      batch->GetInt("expires_at") !=
          stored.GetInt("created_at") + kBatchWindowSeconds ||
      !ValidBatchEndpoint(batch->GetString("endpoint")) ||
      batch->GetString("completion_window") != "24h" ||
      !ValidApplicationId(batch->GetString("input_file_id")) ||
      !Integer(*data, "validated_rows", 0, kBatchRequests) ||
      !Integer(*data, "validated_bytes", 0, manifest.bytes) ||
      !Integer(*data, "output_ttl_seconds", 3600, 30 * 24 * 3600))
    return Corrupt();
  if (pages->array.size() !=
          (data->GetInt("validated_rows") + kBatchPageRows - 1) /
              kBatchPageRows ||
      !Integer(*data, "running", 0, 1) ||
      !Integer(*data, "next_row", 0, data->GetInt("validated_rows")) ||
      !Integer(*data, "recoveries", 0, 1000000))
    return Corrupt();
  for (const auto& page : pages->array)
    if (!page.IsString() || !ValidApplicationId(page.str)) return Corrupt();
  const auto status = batch->GetString("status");
  if (!BatchTerminal(status) && status != "validating" &&
      status != "in_progress" && status != "finalizing" &&
      status != "cancelling")
    return Corrupt();
  const auto* counts = batch->Find("request_counts");
  if (!counts || !Integer(*counts, "total", 0, kBatchRequests) ||
      !Integer(*counts, "completed", 0, kBatchRequests) ||
      !Integer(*counts, "failed", 0, kBatchRequests) ||
      counts->GetInt("completed") + counts->GetInt("failed") >
          counts->GetInt("total"))
    return Corrupt();
  ObjectReply result;
  result.value = *batch;
  return result;
}
ObjectReply Commit(ObjectStore& objects, const BinaryAccess& access,
                   const ObjectRequest& request) {
  auto reply = objects.Execute(request, access.deadline, access.cancelled);
  if (reply.ok()) return Public(reply.value);
  Identify(&reply, request.id);
  return reply;
}
void CleanupRead(ObjectStore& objects, BinaryRead* read) noexcept {
  if (read->id.empty() || read->closed || read->commit_unknown) return;
  try {
    BinaryAccess access{
        read->scope, Clock::now() + std::chrono::milliseconds(250), {}};
    if (!BinaryStore(objects).Close(access, read).ok())
      objects.RecordAuxiliaryCleanupFailure();
  } catch (...) {
    objects.RecordAuxiliaryCleanupFailure();
  }
}
}  // namespace

bool BatchTerminal(std::string_view status) {
  return status == "completed" || status == "failed" || status == "expired" ||
         status == "cancelled";
}

ObjectReply BatchState::Create(const BinaryAccess& access,
                               const BatchCreate& options) {
  // Revalidate typed inputs; a caller cannot bypass the public contract.
  auto input =
      Object({{"input_file_id", String(options.input_file_id)},
              {"endpoint", String(options.endpoint)},
              {"completion_window", String("24h")},
              {"metadata", options.metadata},
              {"output_expires_after",
               Object({{"anchor", String("created_at")},
                       {"seconds", Number(options.output_ttl_seconds)}})}});
  BatchCreate checked;
  auto reply = ParseBatchCreate(input, &checked);
  if (!reply.ok()) return reply;
  Json owner;
  reply = Owner(&owner);
  if (!reply.ok()) return reply;
  reply = Files(objects_).Retrieve(access, options.input_file_id);
  if (!reply.ok()) return reply;
  if (reply.value.GetString("purpose") != "batch")
    return Invalid("Input file must have purpose batch", "input_file_id");
  if (reply.value.GetInt("bytes") < 1 ||
      reply.value.GetInt("bytes") > static_cast<int64_t>(kBatchInputBytes))
    return Invalid("Batch file must contain 1..200000000 bytes",
                   "input_file_id");
  BinaryStore binary(objects_);
  BinaryRead read;
  reply = binary.Open(access, ObjectKind::kFile, options.input_file_id, 3600,
                      &read);
  if (!reply.ok()) return reply;
  ScopeExit cleanup([&] { CleanupRead(objects_, &read); });
  const auto id = NewObjectId(ObjectKind::kBatch);
  if (id.empty())
    return Error(503, "object_entropy_unavailable", "No secure batch ID");
  const auto now = std::time(nullptr);
  auto root = Request(access, id, ObjectOperation::kPut);
  root.created_at = now;
  root.data =
      Object({{"format", String("q4t.batch.v1")},
              {"scope", String(access.scope)},
              {"owner", std::move(owner)},
              {"blob", BinaryManifestJson(read.manifest)},
              {"pages", Array()},
              {"validated_rows", Number(0)},
              {"validated_bytes", Number(0)},
              {"running", Number(0)},
              {"next_row", Number(0)},
              {"recoveries", Number(0)},
              {"output_ttl_seconds", Number(options.output_ttl_seconds)},
              {"batch",
               Object({{"id", String(id)},
                       {"object", String("batch")},
                       {"status", String("validating")},
                       {"endpoint", String(options.endpoint)},
                       {"input_file_id", String(options.input_file_id)},
                       {"completion_window", String("24h")},
                       {"created_at", Number(now)},
                       {"expires_at", Number(now + kBatchWindowSeconds)},
                       {"metadata", options.metadata},
                       {"output_file_id", Json()},
                       {"error_file_id", Json()},
                       {"errors", Json()},
                       {"request_counts", Object({{"total", Number(0)},
                                                  {"completed", Number(0)},
                                                  {"failed", Number(0)}})}})}});
  // A still-live, exact source pin participates in the same transaction. The
  // worker's mutation deadline prevents pruning and then publishing a dangling
  // manifest even when caller scheduling was delayed beyond the pin lifetime.
  auto pin = Request(access, read.id, ObjectOperation::kPut);
  pin.kind = ObjectKind::kToolRun;
  pin.expected_revision = 1;
  pin.expires_at = read.expires;
  pin.mutate_before = read.expires;
  pin.data = Object({{"format", String("q4t.binary_read.v1")},
                     {"blob", BinaryManifestJson(read.manifest)}});
  std::array mutations{std::move(pin), std::move(root)};
  reply = objects_.ExecuteBatch(mutations, access.deadline, access.cancelled);
  if (reply.commit_unknown) cleanup.Release();
  if (!reply.ok()) {
    Identify(&reply, id);
    return reply;
  }
  // Use the known committed data instead of turning an auxiliary read into
  // another acceptance decision after the atomic write succeeded.
  ObjectReply result;
  result.value = *mutations[1].data.Find("batch");
  return result;
}

ObjectReply BatchState::Load(const BinaryAccess& access,
                             const std::string& id) {
  if (access.scope.empty() || !ValidApplicationId(id))
    return Invalid("Invalid batch scope or ID");
  auto reply = objects_.Execute(Request(access, id, ObjectOperation::kGet),
                                access.deadline, access.cancelled);
  if (!reply.ok()) return reply;
  auto public_reply = Public(reply.value);
  if (!public_reply.ok()) return public_reply;
  if (reply.value.Find("data")->GetString("scope") != access.scope)
    return Corrupt();
  return reply;
}
ObjectReply BatchState::Retrieve(const BinaryAccess& access,
                                 const std::string& id) {
  auto reply = Load(access, id);
  return reply.ok() ? Public(reply.value) : reply;
}
ObjectReply BatchState::Owned(const BinaryAccess& access,
                              const std::string& id) {
  auto reply = Load(access, id);
  if (!reply.ok()) return reply;
  Json owner;
  auto checked = Owner(&owner);
  if (!checked.ok()) return checked;
  const auto* recorded = reply.value.Find("data")->Find("owner");
  if (!recorded || OutputJson(*recorded) != OutputJson(owner))
    return Error(409, "batch_owner_changed", "Batch execution owner differs");
  return reply;
}
ObjectReply BatchState::List(const BinaryAccess& access, const Json& query) {
  std::string param;
  auto checked = InputFields(query, {"after", "limit"}, &param, "query");
  if (!checked) return Invalid(checked.message(), param);
  auto request = Request(access, "", ObjectOperation::kList);
  request.limit = 20;
  if (const auto* limit = query.Find("limit")) {
    if (!IsInputInteger(*limit) || limit->AsInt() < 1 || limit->AsInt() > 100)
      return Invalid("limit requires 1..100", "limit");
    request.limit = limit->AsInt();
  }
  if (const auto* after = query.Find("after")) {
    if (!after->IsString() || !ValidApplicationId(after->str))
      return Invalid("Invalid batch cursor", "after");
    request.after = after->str;
  }
  auto reply = objects_.Execute(request, access.deadline, access.cancelled);
  if (!reply.ok()) return reply;
  auto* data = &Field(reply.value, "data");
  if (!data->IsArray()) return Corrupt();
  for (auto& stored : data->array) {
    auto item = Public(stored);
    if (!item.ok()) return item;
    stored = std::move(item.value);
  }
  return reply;
}

ObjectReply BatchState::AppendValidationPage(
    const BinaryAccess& access, const std::string& id,
    std::span<const BatchRowPosition> rows) {
  auto loaded = Owned(access, id);
  if (!loaded.ok()) return loaded;
  auto root = Update(access, loaded.value);
  const auto* batch = root.data.Find("batch");
  if (batch->GetString("status") != "validating")
    return Error(409, "batch_state_conflict", "Batch is not validating");
  auto count = root.data.GetInt("validated_rows");
  auto offset = root.data.GetInt("validated_bytes");
  const auto total_bytes = root.data.Find("blob")->GetInt("bytes");
  if (rows.empty() || rows.size() > kBatchPageRows || count % kBatchPageRows ||
      rows.size() > kBatchRequests - count)
    return Invalid("Invalid batch validation page");
  auto entries = Array();
  for (const auto& row : rows) {
    if (row.line != static_cast<uint64_t>(count + 1) ||
        row.offset != static_cast<uint64_t>(offset) || !row.wire_bytes ||
        row.wire_bytes > kChatBodyBytes + 1 ||
        row.wire_bytes > static_cast<uint64_t>(total_bytes - offset) ||
        !Hash(row.custom_id_sha256))
      return Invalid("Batch row does not match original input position");
    entries.array.push_back(
        Object({{"line", Number(row.line)},
                {"offset", Number(row.offset)},
                {"wire_bytes", Number(row.wire_bytes)},
                {"custom_id_sha256", String(row.custom_id_sha256)},
                {"status", String("pending")},
                {"result_id", Json()}}));
    ++count;
    offset += row.wire_bytes;
  }
  const auto page_id = NewObjectId(ObjectKind::kToolRun);
  if (page_id.empty())
    return Error(503, "object_entropy_unavailable", "No page ID");
  auto page = Request(access, page_id, ObjectOperation::kPut);
  page.kind = ObjectKind::kToolRun;
  page.ttl_seconds = 0;
  page.data = Object({{"format", String("q4t.batch_page.v1")},
                      {"batch_id", String(id)},
                      {"rows", std::move(entries)}});
  Field(root.data, "pages").array.push_back(String(page_id));
  Set(&root.data, "validated_rows", Number(count));
  Set(&root.data, "validated_bytes", Number(offset));
  root.mutate_before = batch->GetInt("expires_at");
  std::array mutations{std::move(page), std::move(root)};
  auto reply =
      objects_.ExecuteBatch(mutations, access.deadline, access.cancelled);
  if (reply.ok()) {
    reply.value = Object({{"id", String(id)},
                          {"page_id", String(page_id)},
                          {"validated_rows", Number(count)}});
  } else {
    Identify(&reply, id);
    Set(&reply.value, "page_id", String(page_id));
  }
  return reply;
}
ObjectReply BatchState::CompleteValidation(const BinaryAccess& access,
                                           const std::string& id,
                                           const std::string& canonical_model) {
  auto loaded = Owned(access, id);
  if (!loaded.ok()) return loaded;
  auto root = Update(access, loaded.value);
  auto& batch = Field(root.data, "batch");
  if (batch.GetString("status") != "validating")
    return Error(409, "batch_state_conflict", "Batch is not validating");
  if (canonical_model.empty() || canonical_model.size() > 256 ||
      root.data.GetInt("validated_rows") < 1 ||
      root.data.GetInt("validated_bytes") !=
          root.data.Find("blob")->GetInt("bytes"))
    return Invalid("Batch file validation is incomplete");
  root.mutate_before = batch.GetInt("expires_at");
  Set(&batch, "model", String(canonical_model));
  Set(&batch, "status", String("in_progress"));
  Set(&batch, "in_progress_at", Number(std::time(nullptr)));
  Set(&Field(batch, "request_counts"), "total",
      Number(root.data.GetInt("validated_rows")));
  return Commit(objects_, access, root);
}
ObjectReply BatchState::FailValidation(const BinaryAccess& access,
                                       const std::string& id,
                                       const ObjectReply& error) {
  if (error.ok()) return Invalid("Missing batch validation error");
  auto loaded = Owned(access, id);
  if (!loaded.ok()) return loaded;
  auto root = Update(access, loaded.value);
  auto& batch = Field(root.data, "batch");
  if (batch.GetString("status") != "validating")
    return Error(409, "batch_state_conflict", "Batch is not validating");
  Set(&batch, "status", String("failed"));
  Set(&batch, "failed_at", Number(std::time(nullptr)));
  Set(&batch, "errors",
      Object({{"object", String("list")},
              {"data",
               Array({Object({{"code", String(std::string(error.ClientCode()))},
                              {"message", String(error.ClientMessage())},
                              {"line", error.value.Find("line")
                                           ? *error.value.Find("line")
                                           : Json()},
                              {"param", error.value.Find("param")
                                            ? *error.value.Find("param")
                                            : Json()}})})}}));
  return Commit(objects_, access, root);
}
ObjectReply BatchState::Cancel(const BinaryAccess& access,
                               const std::string& id) {
  auto loaded = Load(access, id);
  if (!loaded.ok()) return loaded;
  auto root = Update(access, loaded.value);
  auto& batch = Field(root.data, "batch");
  const auto status = batch.GetString("status");
  if (status == "cancelling" || status == "cancelled")
    return Public(loaded.value);
  if (BatchTerminal(status))
    return Error(409, "batch_already_terminal", "Batch is already terminal");
  Set(&batch, "status", String("cancelling"));
  Set(&batch, "cancelling_at", Number(std::time(nullptr)));
  return Commit(objects_, access, root);
}
ObjectReply BatchState::FinishCancelled(const BinaryAccess& access,
                                        const std::string& id) {
  auto loaded = Owned(access, id);
  if (!loaded.ok()) return loaded;
  auto root = Update(access, loaded.value);
  auto& batch = Field(root.data, "batch");
  if (batch.GetString("status") != "cancelling" ||
      root.data.GetInt("running", -1) != 0)
    return Error(409, "batch_state_conflict",
                 "Batch cancellation still has execution owners");
  Set(&batch, "status", String("cancelled"));
  Set(&batch, "cancelled_at", Number(std::time(nullptr)));
  return Commit(objects_, access, root);
}
ObjectReply BatchState::ClaimAbandoned(const BinaryAccess& access,
                                       const std::string& id) {
  auto loaded = Load(access, id);
  if (!loaded.ok()) return loaded;
  auto root = Update(access, loaded.value);
  const auto* recorded = root.data.Find("owner");
  if (!recorded || ResponseOwnerStatus(*recorded) != ResponseOwnerState::kGone)
    return Error(409, "batch_owner_unconfirmed",
                 "Prior execution owner is not confirmed gone");
  if (BatchTerminal(root.data.Find("batch")->GetString("status")))
    return Error(409, "batch_already_terminal", "Batch is already terminal");
  if (root.data.GetInt("recoveries") >= 1000000)
    return Error(503, "batch_recovery_limit",
                 "Batch recovery counter exhausted");
  Json owner;
  auto reply = Owner(&owner);
  if (!reply.ok()) return reply;
  Set(&root.data, "owner", std::move(owner));
  Set(&root.data, "recoveries", Number(root.data.GetInt("recoveries") + 1));
  // Do not reset running rows, counters, absolute expiry or original bytes.
  // The executor must reconcile any started row before admitting more work.
  return Commit(objects_, access, root);
}
ObjectReply BatchState::ResetValidation(const BinaryAccess& access,
                                        const std::string& id) {
  auto loaded = Owned(access, id);
  if (!loaded.ok()) return loaded;
  auto root = Update(access, loaded.value);
  const auto* batch = root.data.Find("batch");
  if (batch->GetString("status") != "validating")
    return Error(409, "batch_state_conflict", "Batch is not validating");
  root.mutate_before = batch->GetInt("expires_at");
  Set(&root.data, "pages", Array());
  Set(&root.data, "validated_rows", Number(0));
  Set(&root.data, "validated_bytes", Number(0));
  return Commit(objects_, access, root);
}
}  // namespace q4t::server
