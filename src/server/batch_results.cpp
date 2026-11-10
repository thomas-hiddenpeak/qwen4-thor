#include "q4t/server/batch_state.h"
#include "q4t/server/batch_quota.h"

#include <algorithm>
#include <array>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/batch_checkpoint.h"
#include "q4t/server/batch_usage.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_owner.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
ObjectReply Corrupt() {
  return Error(503, "batch_state_invalid",
               "Stored batch checkpoint is invalid");
}
ObjectReply Conflict() {
  return Error(409, "batch_state_conflict",
               "Batch checkpoint does not permit this transition");
}
ObjectRequest Request(const BinaryAccess& access, const std::string& id,
                      ObjectKind kind,
                      ObjectOperation operation = ObjectOperation::kGet) {
  ObjectRequest request;
  request.scope = access.scope;
  request.id = id;
  request.kind = kind;
  request.operation = operation;
  return request;
}
ObjectRequest Update(const BinaryAccess& access, const Json& stored,
                     ObjectKind kind) {
  auto request =
      Request(access, stored.GetString("id"), kind, ObjectOperation::kPut);
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
ObjectReply Save(ObjectStore& objects, const BinaryAccess& access,
                 const ObjectRequest& root) {
  auto result = objects.Execute(root, access.deadline, access.cancelled);
  if (result.ok())
    result.value = *root.data.Find("batch");
  else
    Identify(&result, root.id);
  return result;
}
void Close(ObjectStore& objects, BinaryRead* read) noexcept {
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
void Abort(ObjectStore& objects, BinaryStage* stage) noexcept {
  if (stage->id.empty() || stage->closed || stage->commit_unknown) return;
  try {
    BinaryAccess access{
        stage->scope, Clock::now() + std::chrono::milliseconds(250), {}};
    if (!BinaryStore(objects).Abort(access, stage).ok())
      objects.RecordAuxiliaryCleanupFailure();
  } catch (...) {
    objects.RecordAuxiliaryCleanupFailure();
  }
}
bool Integer(const Json& object, const std::string& key, int64_t min,
             int64_t max) {
  const auto* value = object.Find(key);
  return value && IsInputInteger(*value) && value->AsInt() >= min &&
         value->AsInt() <= max;
}
bool Hash(std::string_view hash) {
  return hash.size() == 64 && std::all_of(hash.begin(), hash.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}
std::string Identity(const char* prefix) {
  auto id = NewObjectId(ObjectKind::kToolRun);
  return id.empty() ? id : std::string(prefix) + id;
}
Json& Entry(ObjectRequest& page, size_t index) {
  return Field(page.data, "rows").array[index % kBatchPageRows];
}
ObjectReply JsonLine(const std::string& custom_id, const BatchRowResult& result,
                     std::string_view endpoint, std::string* line,
                     std::optional<BatchUsage>* usage) {
  const auto id = Identity("batch_req_");
  if (id.empty())
    return Error(503, "object_entropy_unavailable", "No result ID");
  if (result.request_id.size() > 128 ||
      result.response_body.size() > kChatBodyBytes ||
      custom_id.size() > kChatBodyBytes || result.error.code.size() > 128 ||
      result.error.message.size() > 16384)
    return Error(413, "batch_result_capacity_exceeded",
                 "Batch result capacity exceeded");
  std::string response = "null", error = "null";
  if (result.error.ok()) {
    Json body;
    if (result.http_status < 200 || result.http_status > 599 ||
        (result.http_status >= 300 && result.http_status < 400) ||
        result.request_id.empty() ||
        !io::ParseJson(result.response_body, &body, true,
                       kApplicationJsonLimits) ||
        !body.IsObject())
      return Error(500, "batch_response_invalid",
                   "Endpoint did not return a finite JSON response");
    // Formatting newlines in endpoint JSON must not split one JSONL result.
    const auto compact = OutputJson(body);
    *usage = BatchResponseUsage(endpoint, body);
    if (compact.size() > kChatBodyBytes)
      return Error(413, "batch_result_capacity_exceeded",
                   "Batch result capacity exceeded");
    response = "{\"status_code\":" + std::to_string(result.http_status) +
               ",\"request_id\":" + OutputJson(String(result.request_id)) +
               ",\"body\":" + compact + "}";
  } else {
    error = OutputJson(
        Object({{"code", String(std::string(result.error.ClientCode()))},
                {"message", String(result.error.ClientMessage())}}));
  }
  *line = "{\"id\":" + OutputJson(String(id)) +
          ",\"custom_id\":" + OutputJson(String(custom_id)) +
          ",\"response\":" + response + ",\"error\":" + error + "}\n";
  if (line->size() > kExpandedApplicationBytes + 65536)
    return Error(413, "batch_result_capacity_exceeded",
                 "Batch result capacity exceeded");
  return {};
}
}  // namespace

ObjectReply BatchState::Page(const BinaryAccess& access, const Json& stored,
                             size_t index) {
  const auto& data = *stored.Find("data");
  const auto count = static_cast<size_t>(data.GetInt("validated_rows"));
  if (index >= count || data.GetBool("resources_released")) return Conflict();
  const auto id = data.GetArray("pages")->array[index / kBatchPageRows].str;
  auto page = objects_.Execute(Request(access, id, ObjectKind::kToolRun),
                               access.deadline, access.cancelled);
  if (!page.ok()) return page;
  const auto* content = page.value.Find("data");
  const auto* rows = content ? content->GetArray("rows") : nullptr;
  const size_t first = index / kBatchPageRows * kBatchPageRows;
  if (!content || !IsBatchPage(*content) ||
      content->GetString("batch_id") != stored.GetString("id") || !rows ||
      rows->array.size() != std::min(kBatchPageRows, count - first))
    return Corrupt();
  uint64_t end = 0;
  const auto bytes = data.Find("blob")->GetInt("bytes");
  for (size_t i = 0; i < rows->array.size(); ++i) {
    const auto& row = rows->array[i];
    const auto* result = row.Find("result_id");
    BatchInputBudget budget;
    if (const auto* input = row.Find("input_budget");
        input && !ParseBatchInputBudget(*input, &budget))
      return Corrupt();
    BatchInputRecount recount;
    if (const auto* value = data.Find("input_recount");
        value && !ParseBatchInputRecount(*value, &recount))
      return Corrupt();
    if ((data.Has("enqueued_input_tokens") || first + i < recount.rows) !=
        row.Has("input_budget"))
      return Corrupt();
    const auto status = row.GetString("status");
    if (!Integer(row, "line", first + i + 1, first + i + 1) ||
        !Integer(row, "offset", 0, bytes - 1) ||
        !Integer(row, "wire_bytes", 1, kChatBodyBytes + 1) ||
        row.GetInt("wire_bytes") > bytes - row.GetInt("offset") ||
        (i && static_cast<uint64_t>(row.GetInt("offset")) != end) ||
        (!first && !i && row.GetInt("offset") != 0) ||
        !Hash(row.GetString("custom_id_sha256")) || !result ||
        (status != "pending" && status != "running" && status != "completed" &&
         status != "failed") ||
        ((status == "completed" || status == "failed")
             ? (row.Has("result_inline")
                    ? (!result->IsNull() || !ValidBatchInlineResult(row))
                    : (!result->IsString() || !ValidApplicationId(result->str)))
             : !result->IsNull()) ||
        ((status == "pending" || status == "running") &&
         row.Has("result_inline")) ||
        (status == "running" &&
         !ValidApplicationId(row.GetString("request_id"))))
      return Corrupt();
    if (const auto* embedded = row.Find("prepared_inline");
        embedded &&
        (row.Has("input_snapshots") || !ValidBatchInlineInput(*embedded)))
      return Corrupt();
    if ((row.Has("prepared_inline") || row.Has("result_inline")) &&
        content->GetString("format") != "q4t.batch_page.v2")
      return Corrupt();
    end = row.GetInt("offset") + row.GetInt("wire_bytes");
    if (const auto* owner = row.Find("execution_owner");
        owner && !ValidResponseOwner(*owner))
      return Corrupt();
    if (const auto* background = row.Find("background_response")) {
      BatchResponseLink link;
      if (!ParseBatchResponseLink(*background, &link) ||
          link.batch_id != stored.GetString("id") ||
          link.scope != access.scope || link.index != first + i ||
          link.request_id != row.GetString("request_id") || status == "pending")
        return Corrupt();
    }
    if (const auto* frozen = row.Find("input_snapshots")) {
      InputSnapshotRef reference;
      if (!frozen->IsArray() || frozen->array.size() != 1 ||
          !frozen->array[0].Find("snapshot") ||
          !ParseInputSnapshotReference(*frozen->array[0].Find("snapshot"),
                                       &reference) ||
          !Hash(frozen->array[0].GetString("binding_sha256")))
        return Corrupt();
    }
  }
  if (first + rows->array.size() == count &&
      end != static_cast<uint64_t>(data.GetInt("validated_bytes")))
    return Corrupt();
  return page;
}

ObjectReply BatchState::Row(const BinaryAccess& access, const std::string& id,
                            size_t index) {
  auto loaded = Load(access, id);
  if (!loaded.ok()) return loaded;
  auto page = Page(access, loaded.value, index);
  if (!page.ok()) return page;
  ObjectReply reply;
  reply.value =
      page.value.Find("data")->GetArray("rows")->array[index % kBatchPageRows];
  return reply;
}

ObjectReply BatchState::ReadInputRow(const BinaryAccess& access,
                                     const std::string& id, size_t index,
                                     BatchInputRow* row) {
  if (!row) return Error(500, "batch_output_missing", "Missing row output");
  auto loaded = Load(access, id);
  if (!loaded.ok()) return loaded;
  auto page = Page(access, loaded.value, index);
  if (!page.ok()) return page;
  const auto& position =
      page.value.Find("data")->GetArray("rows")->array[index % kBatchPageRows];
  BinaryRead read;
  BinaryStore binary(objects_);
  auto reply = binary.Open(access, ObjectKind::kBatch, id, 3600, &read);
  if (!reply.ok()) return reply;
  ScopeExit cleanup([&] { Close(objects_, &read); });
  const uint64_t start = position.GetInt("offset");
  const uint64_t finish = start + position.GetInt("wire_bytes");
  BatchInputRow restored;
  const auto& batch = *loaded.value.Find("data")->Find("batch");
  BatchInput input(
      batch.GetString("endpoint"),
      [&](const auto&, auto* check) {
        check->canonical_model = batch.GetString("model", "validated-input");
        return ObjectReply{};
      },
      [&](auto&& value) {
        restored = std::move(value);
        return ObjectReply{};
      },
      {.max_requests = 1}, access.cancelled);
  uint64_t offset = 0;
  for (size_t i = 0; i < read.manifest.chunks.size() && offset < finish; ++i) {
    const auto next = offset + read.manifest.chunks[i].bytes;
    if (next > start) {
      std::string chunk;
      reply = binary.Read(access, read, i, &chunk);
      if (!reply.ok()) return reply;
      const auto lo = std::max(offset, start), hi = std::min(next, finish);
      reply = input.Feed(std::string_view(chunk).substr(lo - offset, hi - lo));
      if (!reply.ok())
        return reply.code == "batch_cancelled" ? reply : Corrupt();
    }
    offset = next;
  }
  reply = input.Finish();
  if (!reply.ok() && reply.code == "batch_cancelled") return reply;
  if (!reply.ok() || input.Rows() != 1 || input.Bytes() != finish - start ||
      Digest(restored.custom_id) != position.GetString("custom_id_sha256"))
    return Corrupt();
  restored.line = index + 1;
  restored.offset = start;
  *row = std::move(restored);
  return {};
}

ObjectReply BatchState::StartRow(const BinaryAccess& access,
                                 const std::string& id, size_t index,
                                 const std::string& reserved_request_id,
                                 const BatchResponseLink* background) {
  if (!reserved_request_id.empty() &&
      (!reserved_request_id.starts_with("req_tool_run_") ||
       reserved_request_id.size() != 45 ||
       !ValidApplicationId(reserved_request_id)))
    return Error(500, "batch_request_identity_invalid",
                 "Invalid reserved execution identity");
  BatchResponseLink checked;
  if (background &&
      (!ParseBatchResponseLink(BatchResponseLinkValue(*background), &checked) ||
       background->scope != access.scope || background->batch_id != id ||
       background->index != index ||
       background->request_id != reserved_request_id))
    return Error(500, "batch_response_binding_invalid",
                 "Invalid reserved background execution identity");
  auto loaded = Owned(access, id);
  if (!loaded.ok()) return loaded;
  auto root = Update(access, loaded.value, ObjectKind::kBatch);
  if (root.data.Find("batch")->GetString("status") != "in_progress" ||
      root.data.GetInt("running") ||
      static_cast<size_t>(root.data.GetInt("next_row")) != index)
    return Conflict();
  if (BatchUsageRows(root.data) != index)
    return Error(503, "batch_usage_recount_required",
                 "Original results require usage reconciliation");
  auto loaded_page = Page(access, loaded.value, index);
  if (!loaded_page.ok()) return loaded_page;
  auto page = Update(access, loaded_page.value, ObjectKind::kToolRun);
  auto& row = Entry(page, index);
  if (row.GetString("status") != "pending") return Conflict();
  const auto request_id = reserved_request_id.empty() ? Identity("req_")
                                                       : reserved_request_id;
  if (request_id.empty())
    return Error(503, "object_entropy_unavailable", "No request ID");
  Set(&row, "status", String("running"));
  Set(&row, "request_id", String(request_id));
  Set(&row, "started_at", Number(std::time(nullptr)));
  // A later root takeover must not erase the identity of the process which
  // may have executed this ticket. Unknown or live row owners cannot replay.
  Set(&row, "execution_owner", *root.data.Find("owner"));
  if (background)
    Set(&row, "background_response", BatchResponseLinkValue(*background));
  Set(&root.data, "running", Number(1));
  root.mutate_before = root.data.Find("batch")->GetInt("expires_at");
  const auto receipt = row;
  const std::array mutations{std::move(page), std::move(root)};
  auto reply =
      objects_.ExecuteBatch(mutations, access.deadline, access.cancelled);
  if (reply.ok())
    reply.value = receipt;
  else {
    Identify(&reply, id);
    Set(&reply.value, "request_id", String(request_id));
    Set(&reply.value, "index", Number(index));
  }
  return reply;
}

ObjectReply BatchState::FinishRow(const BinaryAccess& access,
                                  const std::string& id, size_t index,
                                  const std::string& custom_id,
                                  const BatchRowResult& result) {
  auto loaded = Owned(access, id);
  if (!loaded.ok()) return loaded;
  auto root = Update(access, loaded.value, ObjectKind::kBatch);
  const auto& batch = *root.data.Find("batch");
  const auto status = batch.GetString("status");
  if (BatchTerminal(status) || status == "validating" ||
      static_cast<size_t>(root.data.GetInt("next_row")) != index)
    return Conflict();
  auto loaded_page = Page(access, loaded.value, index);
  if (!loaded_page.ok()) return loaded_page;
  auto page = Update(access, loaded_page.value, ObjectKind::kToolRun);
  auto& row = Entry(page, index);
  if (Digest(custom_id) != row.GetString("custom_id_sha256")) return Corrupt();
  const bool running = row.GetString("status") == "running";
  if (running) {
    if (root.data.GetInt("running") != 1 ||
        result.request_id != row.GetString("request_id"))
      return Conflict();
  } else if (row.GetString("status") != "pending" ||
             root.data.GetInt("running") || status != "finalizing" ||
             !result.request_id.empty() ||
             !((root.data.GetString("final_status") == "cancelled" &&
                result.error.code == "batch_cancelled") ||
               (root.data.GetString("final_status") == "expired" &&
                result.error.code == "batch_expired")))
    return Conflict();
  std::string line;
  std::optional<BatchUsage> usage;
  auto reply =
      JsonLine(custom_id, result, batch.GetString("endpoint"), &line, &usage);
  if (!reply.ok()) return reply;
  const bool success = result.error.ok() && result.http_status < 300;
  Set(&row, "status", String(success ? "completed" : "failed"));
  Set(&row, "finished_at", Number(std::time(nullptr)));
  Set(&root.data, "running", Number(0));
  Set(&root.data, "next_row", Number(index + 1));
  auto& counts = Field(Field(root.data, "batch"), "request_counts");
  const auto* key = success ? "completed" : "failed";
  Set(&counts, key, Number(counts.GetInt(key) + 1));
  // A never-started cancelled/expired row provably consumed no model tokens.
  // A running failure with no result usage remains unknown, not fabricated 0.
  if (!running) usage = BatchUsage{};
  reply = AddBatchUsage(&root.data, index, usage);
  if (!reply.ok()) return reply;
  if (line.size() <= kBatchInlineBytes) {
    Set(&page.data, "format", String("q4t.batch_page.v2"));
    Set(&row, "result_inline",
        Object({{"sha256", String(Digest(line))}, {"payload", String(line)}}));
    const std::array mutations{std::move(page), std::move(root)};
    reply = objects_.ExecuteBatch(mutations, access.deadline, access.cancelled);
    if (reply.ok())
      reply.value = *mutations[1].data.Find("batch");
    else {
      Identify(&reply, id);
      Set(&reply.value, "result_id", Json());
    }
    return reply;
  }
  BinaryStore binary(objects_);
  BinaryStage stage;
  reply = binary.Begin(access, line.size(), 3600, &stage);
  if (!reply.ok()) return reply;
  ScopeExit cleanup([&] { Abort(objects_, &stage); });
  for (size_t offset = 0; offset < line.size(); offset += kBinaryChunkBytes) {
    reply =
        binary.Append(access, &stage,
                      std::string_view(line).substr(offset, kBinaryChunkBytes));
    if (!reply.ok()) return reply;
  }
  const auto result_id = NewObjectId(ObjectKind::kToolRun);
  if (result_id.empty())
    return Error(503, "object_entropy_unavailable", "No result root ID");
  auto output =
      Request(access, result_id, ObjectKind::kToolRun, ObjectOperation::kPut);
  output.ttl_seconds = 0;
  output.data = Object({{"format", String("q4t.batch_result.v1")},
                        {"batch_id", String(id)},
                        {"line", Number(index + 1)},
                        {"successful", Boolean(success)}});
  Set(&row, "result_id", String(result_id));
  const std::array parents{std::move(page), std::move(root)};
  reply = binary.Publish(access, &stage, std::move(output), parents);
  if (reply.ok())
    reply.value = *parents[1].data.Find("batch");
  else {
    Identify(&reply, id);
    Set(&reply.value, "result_id", String(result_id));
  }
  return reply;
}

ObjectReply BatchState::RecountUsage(const BinaryAccess& access,
                                     const std::string& id) {
  auto loaded = Owned(access, id);
  if (!loaded.ok()) return loaded;
  auto root = Update(access, loaded.value, ObjectKind::kBatch);
  const auto index = BatchUsageRows(root.data);
  const auto next = static_cast<size_t>(root.data.GetInt("next_row"));
  if (index >= next) {
    ObjectReply reply;
    reply.value = *root.data.Find("batch");
    return reply;
  }
  auto page = Page(access, loaded.value, index);
  if (!page.ok()) return page;
  const auto& row =
      page.value.Find("data")->GetArray("rows")->array[index % kBatchPageRows];
  const bool success = row.GetString("status") == "completed";
  if (!success && row.GetString("status") != "failed") return Corrupt();
  std::string line;
  if (const auto* embedded = row.Find("result_inline")) {
    line = embedded->GetString("payload");
  } else {
    BinaryRead read;
    Json result_data;
    BinaryStore binary(objects_);
    auto reply =
        binary.Open(access, ObjectKind::kToolRun, row.GetString("result_id"),
                    3600, &read, &result_data);
    if (!reply.ok()) return reply;
    ScopeExit close([&] { Close(objects_, &read); });
    const auto* successful = result_data.Find("successful");
    if (result_data.GetString("format") != "q4t.batch_result.v1" ||
        result_data.GetString("batch_id") != id ||
        result_data.GetInt("line") != static_cast<int64_t>(index + 1) ||
        !successful || !successful->IsBool() ||
        successful->boolean != success || !read.manifest.bytes ||
        read.manifest.bytes > kExpandedApplicationBytes + 65536)
      return Corrupt();
    line.reserve(read.manifest.bytes);
    for (size_t chunk = 0; chunk < read.manifest.chunks.size(); ++chunk) {
      std::string bytes;
      reply = binary.Read(access, read, chunk, &bytes);
      if (!reply.ok()) return reply;
      line += bytes;
    }
  }
  Json result;
  auto limits = kApplicationJsonLimits;
  limits.max_depth += 4;  // Private JSONL wrapper around the original body.
  limits.max_values += 32;
  limits.max_string_bytes = kExpandedApplicationBytes + 65536;
  if (!io::ParseJson(line, &result, true, limits) ||
      Digest(result.GetString("custom_id")) !=
          row.GetString("custom_id_sha256"))
    return Corrupt();
  std::optional<BatchUsage> usage;
  const auto* response = result.Find("response");
  const auto* body = response ? response->Find("body") : nullptr;
  if (body)
    usage = BatchResponseUsage(root.data.Find("batch")->GetString("endpoint"),
                               *body);
  else if (row.GetString("request_id").empty()) {
    const auto* error = result.Find("error");
    const auto code = error ? error->GetString("code") : "";
    if (code == "batch_cancelled" || code == "batch_expired")
      usage = BatchUsage{};
  }
  auto reply = AddBatchUsage(&root.data, index, usage);
  return reply.ok() ? Save(objects_, access, root) : reply;
}

ObjectReply BatchState::BeginFinalization(const BinaryAccess& access,
                                          const std::string& id,
                                          const std::string& terminal_status) {
  auto loaded = Owned(access, id);
  if (!loaded.ok()) return loaded;
  auto root = Update(access, loaded.value, ObjectKind::kBatch);
  auto& batch = Field(root.data, "batch");
  const auto status = batch.GetString("status");
  if (status == "finalizing" &&
      root.data.GetString("final_status") == terminal_status) {
    ObjectReply result;
    result.value = batch;
    return result;
  }
  if (BatchTerminal(status) || root.data.GetInt("running")) return Conflict();
  if (BatchUsageRows(root.data) !=
      static_cast<size_t>(root.data.GetInt("next_row")))
    return Error(503, "batch_usage_recount_required",
                 "Original results require usage reconciliation");
  if (terminal_status == "completed") {
    if (status != "in_progress" ||
        root.data.GetInt("next_row") !=
            batch.Find("request_counts")->GetInt("total"))
      return Conflict();
  } else if (terminal_status == "cancelled") {
    if (status != "cancelling") return Conflict();
  } else if (terminal_status == "expired") {
    if (status == "cancelling" ||
        std::time(nullptr) < batch.GetInt("expires_at"))
      return Conflict();
  } else
    return Conflict();
  if (batch.Find("request_counts")->GetInt("total") == 0) {
    Set(&batch, "status", String(terminal_status));
    Set(&batch, terminal_status + "_at", Number(std::time(nullptr)));
    Set(&root.data, "resources_released", Boolean(true));
  } else {
    Set(&batch, "status", String("finalizing"));
    Set(&batch, "finalizing_at", Number(std::time(nullptr)));
    Set(&root.data, "final_status", String(terminal_status));
  }
  return Save(objects_, access, root);
}

ObjectReply BatchState::Finalize(const BinaryAccess& access,
                                 const std::string& id) {
  auto loaded = Owned(access, id);
  if (!loaded.ok()) return loaded;
  auto root = Update(access, loaded.value, ObjectKind::kBatch);
  auto& batch = Field(root.data, "batch");
  const auto terminal = root.data.GetString("final_status");
  const auto* counts = batch.Find("request_counts");
  const auto total = counts->GetInt("total");
  if (batch.GetString("status") != "finalizing" ||
      root.data.GetInt("running") || root.data.GetInt("next_row") != total ||
      counts->GetInt("completed") + counts->GetInt("failed") != total ||
      (terminal != "completed" && terminal != "cancelled" &&
       terminal != "expired"))
    return Conflict();
  if (BatchUsageRows(root.data) != static_cast<size_t>(total))
    return Error(503, "batch_usage_recount_required",
                 "Original results require usage reconciliation");
  // A checkpoint owns the output chunks directly. Temporary staging roots
  // exist only within this turn, and disappear atomically with progress.
  // Recovery never needs an expired in-memory stage or an earlier model run.
  std::array<BinaryManifest, 2> manifests;
  std::array<int64_t, 2> observed{};
  int64_t index = 0;
  size_t source_chunk = 0;
  if (const auto* progress = root.data.Find("finalization")) {
    if (progress->GetString("format") != "q4t.batch_finalization.v1" ||
        !Integer(*progress, "next_row", 0, total) ||
        !Integer(*progress, "source_chunk", 0, kBinaryMaxChunks) ||
        !Integer(*progress, "completed", 0, counts->GetInt("completed")) ||
        !Integer(*progress, "failed", 0, counts->GetInt("failed")) ||
        !progress->Find("output") || !progress->Find("error") ||
        !ParseBinaryManifest(*progress->Find("output"), &manifests[0]) ||
        !ParseBinaryManifest(*progress->Find("error"), &manifests[1]))
      return Corrupt();
    index = progress->GetInt("next_row");
    source_chunk = progress->GetInt("source_chunk");
    observed = {progress->GetInt("completed"), progress->GetInt("failed")};
    if (observed[0] + observed[1] != index || (index == total && source_chunk))
      return Corrupt();
  }
  BinaryStore binary(objects_);
  std::array<BinaryStage, 2> stages;
  std::array<std::string, 2> buffers;
  ScopeExit cleanup([&] {
    for (auto& stage : stages) Abort(objects_, &stage);
  });
  const auto flush = [&](size_t lane) -> ObjectReply {
    if (buffers[lane].empty()) return {};
    if (stages[lane].id.empty()) {
      auto reply = binary.Begin(access, kBinaryMaxBytes, 3600, &stages[lane]);
      if (!reply.ok()) return reply;
    }
    auto reply = binary.Append(access, &stages[lane], buffers[lane]);
    if (reply.ok()) buffers[lane].clear();
    return reply;
  };
  const auto append = [&](size_t lane, std::string_view bytes) -> ObjectReply {
    const auto staged = stages[lane].manifest.bytes + buffers[lane].size();
    if (staged > kBinaryMaxBytes - manifests[lane].bytes ||
        bytes.size() > kBinaryMaxBytes - manifests[lane].bytes - staged)
      return Error(413, "binary_capacity_exceeded",
                   "Batch output File exceeds its byte budget");
    while (!bytes.empty()) {
      const auto n = std::min<size_t>(kBinaryChunkBytes - buffers[lane].size(),
                                      bytes.size());
      buffers[lane].append(bytes.substr(0, n));
      bytes.remove_prefix(n);
      if (buffers[lane].size() == kBinaryChunkBytes) {
        auto reply = flush(lane);
        if (!reply.ok()) return reply;
      }
    }
    return {};
  };
  // Count the bytes consumed from original result manifests, including a
  // partly consumed row. Large rows cannot monopolize a fair worker turn.
  size_t rows = 0;
  uint64_t consumed = 0;
  constexpr uint64_t kTurnBytes = 4 * kBinaryChunkBytes;
  while (index < total && rows < kBatchPageRows && consumed < kTurnBytes) {
    auto page = Page(access, loaded.value, index);
    if (!page.ok()) return page;
    const auto& entries = page.value.Find("data")->GetArray("rows")->array;
    size_t at = index % kBatchPageRows;
    while (at < entries.size() && rows < kBatchPageRows &&
           consumed < kTurnBytes) {
      const auto& row = entries[at];
      const bool success = row.GetString("status") == "completed";
      if (!success && row.GetString("status") != "failed") return Corrupt();
      const size_t lane = success ? 0 : 1;
      if (const auto* embedded = row.Find("result_inline")) {
        if (source_chunk || !ValidBatchInlineResult(row)) return Corrupt();
        const auto text = embedded->GetString("payload");
        if (text.size() > kTurnBytes - consumed) break;
        auto reply = append(lane, text);
        if (!reply.ok()) return reply;
        consumed += text.size();
      } else {
        BinaryRead read;
        Json result_data;
        auto reply =
            binary.Open(access, ObjectKind::kToolRun,
                        row.GetString("result_id"), 3600, &read, &result_data);
        if (!reply.ok()) return reply;
        ScopeExit close([&] { Close(objects_, &read); });
        const auto* successful = result_data.Find("successful");
        if (result_data.GetString("format") != "q4t.batch_result.v1" ||
            result_data.GetString("batch_id") != id ||
            result_data.GetInt("line") != index + 1 || !successful ||
            !successful->IsBool() || successful->boolean != success ||
            !read.manifest.bytes || source_chunk >= read.manifest.chunks.size())
          return Corrupt();
        while (source_chunk < read.manifest.chunks.size()) {
          const auto bytes = read.manifest.chunks[source_chunk].bytes;
          if (bytes > kTurnBytes - consumed) break;
          std::string chunk;
          reply = binary.Read(access, read, source_chunk, &chunk);
          if (!reply.ok()) return reply;
          reply = append(lane, chunk);
          if (!reply.ok()) return reply;
          consumed += chunk.size();
          ++source_chunk;
        }
        if (source_chunk != read.manifest.chunks.size()) break;
        source_chunk = 0;
      }
      ++observed[lane];
      ++index;
      ++at;
      ++rows;
    }
    // A remaining input chunk may not fit the strict byte bound. Commit the
    // consumed prefix and let the next turn start with its full byte budget.
    if (at < entries.size()) break;
  }
  for (size_t lane = 0; lane < stages.size(); ++lane) {
    auto reply = flush(lane);
    if (!reply.ok()) return reply;
    const auto& staged = stages[lane].manifest;
    if (staged.chunks.size() > kBinaryMaxChunks - manifests[lane].chunks.size())
      return Error(413, "binary_capacity_exceeded",
                   "Batch output File exceeds its chunk budget");
    manifests[lane].bytes += staged.bytes;
    manifests[lane].chunks.insert(manifests[lane].chunks.end(),
                                  staged.chunks.begin(), staged.chunks.end());
  }
  const bool done = index == total;
  if (done && (source_chunk || observed[0] != counts->GetInt("completed") ||
               observed[1] != counts->GetInt("failed")))
    return Corrupt();
  Set(&root.data, "finalization",
      Object({{"format", String("q4t.batch_finalization.v1")},
              {"next_row", Number(index)},
              {"source_chunk", Number(source_chunk)},
              {"completed", Number(observed[0])},
              {"failed", Number(observed[1])},
              {"output", BinaryManifestJson(manifests[0])},
              {"error", BinaryManifestJson(manifests[1])}}));
  std::vector<ObjectRequest> mutations;
  if (done) {
    // Setting finalization may reallocate root.data's field vector.
    auto& terminal_batch = Field(root.data, "batch");
    for (size_t lane = 0; lane < manifests.size(); ++lane) {
      if (!manifests[lane].bytes) continue;
      const auto file_id = NewObjectId(ObjectKind::kFile);
      if (file_id.empty())
        return Error(503, "object_entropy_unavailable", "No output File ID");
      auto file =
          Request(access, file_id, ObjectKind::kFile, ObjectOperation::kPut);
      file.created_at = std::time(nullptr);
      file.expires_at =
          file.created_at + root.data.GetInt("output_ttl_seconds");
      file.data = Object(
          {{"format", String("q4t.file.v1")},
           {"mime_type", String("application/jsonl")},
           {"blob", BinaryManifestJson(manifests[lane])},
           {"file", Object({{"id", String(file_id)},
                            {"object", String("file")},
                            {"bytes", Number(manifests[lane].bytes)},
                            {"created_at", Number(file.created_at)},
                            {"expires_at", Number(file.expires_at)},
                            {"filename", String(id + (lane ? "_errors.jsonl"
                                                           : "_output.jsonl"))},
                            {"purpose", String("batch_output")},
                            {"status", String("processed")}})}});
      Set(&terminal_batch, lane ? "error_file_id" : "output_file_id",
          String(file_id));
      mutations.push_back(std::move(file));
    }
    Set(&terminal_batch, "status", String(terminal));
    Set(&terminal_batch, terminal + "_at", Number(std::time(nullptr)));
    Set(&root.data, "resources_released", Boolean(true));
  }
  for (const auto& stage : stages) {
    if (stage.id.empty()) continue;
    auto erase = Request(access, stage.id, ObjectKind::kToolRun,
                         ObjectOperation::kErase);
    erase.expected_revision = stage.revision;
    mutations.push_back(std::move(erase));
  }
  mutations.push_back(std::move(root));
  auto reply =
      objects_.ExecuteBatch(mutations, access.deadline, access.cancelled);
  if (reply.ok()) {
    for (auto& stage : stages) stage.closed = true;
    reply.value = *mutations.back().data.Find("batch");
  } else {
    if (reply.commit_unknown)
      for (auto& stage : stages) stage.commit_unknown = true;
    Identify(&reply, id);
    for (const auto* key : {"output_file_id", "error_file_id"})
      Set(&reply.value, key, *mutations.back().data.Find("batch")->Find(key));
  }
  return reply;
}

}  // namespace q4t::server
