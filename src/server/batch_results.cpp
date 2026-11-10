#include "q4t/server/batch_state.h"

#include <algorithm>
#include <array>
#include <ctime>

#include "object_protocol.h"
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
                     std::string* line) {
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
  if (!content || content->GetString("format") != "q4t.batch_page.v1" ||
      content->GetString("batch_id") != stored.GetString("id") || !rows ||
      rows->array.size() != std::min(kBatchPageRows, count - first))
    return Corrupt();
  uint64_t end = 0;
  const auto bytes = data.Find("blob")->GetInt("bytes");
  for (size_t i = 0; i < rows->array.size(); ++i) {
    const auto& row = rows->array[i];
    const auto* result = row.Find("result_id");
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
             ? (!result->IsString() || !ValidApplicationId(result->str))
             : !result->IsNull()) ||
        (status == "running" &&
         !ValidApplicationId(row.GetString("request_id"))))
      return Corrupt();
    end = row.GetInt("offset") + row.GetInt("wire_bytes");
    if (const auto* owner = row.Find("execution_owner");
        owner && !ValidResponseOwner(*owner))
      return Corrupt();
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
                                 const std::string& reserved_request_id) {
  if (!reserved_request_id.empty() &&
      (!reserved_request_id.starts_with("req_tool_run_") ||
       reserved_request_id.size() != 45 ||
       !ValidApplicationId(reserved_request_id)))
    return Error(500, "batch_request_identity_invalid",
                 "Invalid reserved execution identity");
  auto loaded = Owned(access, id);
  if (!loaded.ok()) return loaded;
  auto root = Update(access, loaded.value, ObjectKind::kBatch);
  if (root.data.Find("batch")->GetString("status") != "in_progress" ||
      root.data.GetInt("running") ||
      static_cast<size_t>(root.data.GetInt("next_row")) != index)
    return Conflict();
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
  auto reply = JsonLine(custom_id, result, &line);
  if (!reply.ok()) return reply;
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
  const bool success = result.error.ok() && result.http_status < 300;
  auto output =
      Request(access, result_id, ObjectKind::kToolRun, ObjectOperation::kPut);
  output.ttl_seconds = 0;
  output.data = Object({{"format", String("q4t.batch_result.v1")},
                        {"batch_id", String(id)},
                        {"line", Number(index + 1)},
                        {"successful", Boolean(success)}});
  Set(&row, "status", String(success ? "completed" : "failed"));
  Set(&row, "result_id", String(result_id));
  Set(&row, "finished_at", Number(std::time(nullptr)));
  Set(&root.data, "running", Number(0));
  Set(&root.data, "next_row", Number(index + 1));
  auto& counts = Field(Field(root.data, "batch"), "request_counts");
  const auto* key = success ? "completed" : "failed";
  Set(&counts, key, Number(counts.GetInt(key) + 1));
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
  BinaryStore binary(objects_);
  std::array<BinaryStage, 2> stages;
  std::array<std::string, 2> buffers;
  std::array<int64_t, 2> observed{};
  ScopeExit cleanup([&] {
    for (auto& stage : stages) Abort(objects_, &stage);
  });
  const auto append = [&](size_t lane, std::string_view bytes) -> ObjectReply {
    if (stages[lane].id.empty()) {
      auto reply = binary.Begin(access, kBinaryMaxBytes, 3600, &stages[lane]);
      if (!reply.ok()) return reply;
    }
    while (!bytes.empty()) {
      const auto n = std::min<size_t>(kBinaryChunkBytes - buffers[lane].size(),
                                      bytes.size());
      buffers[lane].append(bytes.substr(0, n));
      bytes.remove_prefix(n);
      if (buffers[lane].size() == kBinaryChunkBytes) {
        auto reply = binary.Append(access, &stages[lane], buffers[lane]);
        if (!reply.ok()) return reply;
        buffers[lane].clear();
      }
    }
    return {};
  };
  for (int64_t index = 0; index < total;) {
    auto page = Page(access, loaded.value, index);
    if (!page.ok()) return page;
    for (const auto& row : page.value.Find("data")->GetArray("rows")->array) {
      const bool success = row.GetString("status") == "completed";
      if (!success && row.GetString("status") != "failed") return Corrupt();
      ++observed[success ? 0 : 1];
      BinaryRead read;
      Json result_data;
      auto reply =
          binary.Open(access, ObjectKind::kToolRun, row.GetString("result_id"),
                      3600, &read, &result_data);
      if (!reply.ok()) return reply;
      ScopeExit close([&] { Close(objects_, &read); });
      if (result_data.GetString("format") != "q4t.batch_result.v1" ||
          result_data.GetString("batch_id") != id ||
          result_data.GetInt("line") != index + 1 ||
          !result_data.Find("successful") ||
          !result_data.Find("successful")->IsBool() ||
          result_data.GetBool("successful") != success || !read.manifest.bytes)
        return Corrupt();
      for (size_t chunk_index = 0; chunk_index < read.manifest.chunks.size();
           ++chunk_index) {
        std::string chunk;
        reply = binary.Read(access, read, chunk_index, &chunk);
        if (!reply.ok()) return reply;
        reply = append(success ? 0 : 1, chunk);
        if (!reply.ok()) return reply;
      }
      ++index;
    }
  }
  if (observed[0] != counts->GetInt("completed") ||
      observed[1] != counts->GetInt("failed"))
    return Corrupt();
  std::vector<ObjectRequest> files;
  for (size_t lane = 0; lane < stages.size(); ++lane) {
    if (stages[lane].id.empty()) continue;
    if (!buffers[lane].empty()) {
      auto reply = binary.Append(access, &stages[lane], buffers[lane]);
      if (!reply.ok()) return reply;
      buffers[lane].clear();
    }
    const auto file_id = NewObjectId(ObjectKind::kFile);
    if (file_id.empty())
      return Error(503, "object_entropy_unavailable", "No output File ID");
    auto file =
        Request(access, file_id, ObjectKind::kFile, ObjectOperation::kPut);
    file.created_at = std::time(nullptr);
    file.expires_at = file.created_at + root.data.GetInt("output_ttl_seconds");
    file.data = Object(
        {{"format", String("q4t.file.v1")},
         {"mime_type", String("application/jsonl")},
         {"blob", BinaryManifestJson(stages[lane].manifest)},
         {"file", Object({{"id", String(file_id)},
                          {"object", String("file")},
                          {"bytes", Number(stages[lane].manifest.bytes)},
                          {"created_at", Number(file.created_at)},
                          {"expires_at", Number(file.expires_at)},
                          {"filename", String(id + (lane ? "_errors.jsonl"
                                                         : "_output.jsonl"))},
                          {"purpose", String("batch_output")},
                          {"status", String("processed")}})}});
    Set(&batch, lane ? "error_file_id" : "output_file_id", String(file_id));
    files.push_back(std::move(file));
  }
  Set(&batch, "status", String(terminal));
  Set(&batch, terminal + "_at", Number(std::time(nullptr)));
  Set(&root.data, "resources_released", Boolean(true));
  if (files.empty()) return Save(objects_, access, root);
  // The other stage and its File join the primary publication atomically.
  const size_t primary = stages[0].id.empty() ? 1 : 0;
  std::vector<ObjectRequest> parents;
  if (files.size() == 2) {
    parents.push_back(std::move(files[1]));
    auto erase = Request(access, stages[1].id, ObjectKind::kToolRun,
                         ObjectOperation::kErase);
    erase.expected_revision = stages[1].revision;
    parents.push_back(std::move(erase));
  }
  parents.push_back(std::move(root));
  auto reply =
      binary.Publish(access, &stages[primary], std::move(files[0]), parents);
  if (reply.ok()) {
    for (auto& stage : stages) stage.closed = true;
    reply.value = *parents.back().data.Find("batch");
  } else {
    if (reply.commit_unknown)
      for (auto& stage : stages) stage.commit_unknown = true;
    Identify(&reply, id);
    Set(&reply.value, "output_file_id",
        *parents.back().data.Find("batch")->Find("output_file_id"));
    Set(&reply.value, "error_file_id",
        *parents.back().data.Find("batch")->Find("error_file_id"));
  }
  return reply;
}
}  // namespace q4t::server
