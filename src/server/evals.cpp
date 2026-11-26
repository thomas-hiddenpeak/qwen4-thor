#include "q4t/server/evals.h"

#include <algorithm>
#include <chrono>
#include <set>

#include "eval_runs.h"
#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/chat_storage.h"
#include "q4t/server/request_json.h"
#include "q4t/server/schema_graph.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using SortKey = std::pair<int64_t, std::string>;

ObjectReply Invalid(std::string field, std::string message) {
  auto result = Error(400, "invalid_request", std::move(message));
  result.value = Object({{"param", String(std::move(field))}});
  return result;
}

ObjectReply Checked(const Json& value, const char* root) {
  ChatPlanError error;
  auto status = ValidateEval(value, root, &error);
  if (status) return {};
  if (error.http_status == 503) return Error(503, error.code, status.message());
  return Invalid(error.param, status.message());
}

ObjectReply Corrupt() {
  return Error(503, "eval_record_invalid", "Invalid stored evaluation");
}

Json Type(std::string name) {
  return Object({{"type", String(std::move(name))}});
}

Json SampleSchema() {
  auto message = Object({{"type", String("object")},
                         {"properties", Object({{"role", Type("string")}})},
                         {"required", Array({String("role")})}});
  auto choice = Object(
      {{"type", String("object")},
       {"properties", Object({{"index", Type("integer")},
                              {"message", std::move(message)},
                              {"finish_reason", Type("string")}})},
       {"required",
        Array({String("index"), String("message"), String("finish_reason")})}});
  auto choices =
      Object({{"type", String("array")}, {"items", std::move(choice)}});
  const auto objects =
      Object({{"type", String("array")}, {"items", Type("object")}});
  return Object(
      {{"type", String("object")},
       {"properties",
        Object(
            {{"model", Type("string")},
             {"choices", std::move(choices)},
             {"output_text", Type("string")},
             {"output_json", Type("object")},
             {"output_tools", objects},
             {"input_tools", objects},
             {"output_audio",
              Object({{"type", Array({String("object"), String("null")})}})},
             {"output_reasoning_summary",
              Object({{"type", Array({String("string"), String("null")})}})}})},
       {"required", Array({String("model"), String("choices")})}});
}

ObjectReply SourceConfig(const Json& input, Json* out) {
  const bool custom = input.GetString("type") == "custom";
  auto item = Type("object");
  if (custom) {
    item = *input.Find("item_schema");
    OutputSchemaGraph graph;
    std::string param;
    auto valid =
        graph.Init(item, false, &param, "data_source_config.item_schema",
                   OutputSchemaPurpose::kToolResult);
    if (!valid) return Invalid(std::move(param), valid.message());
  }
  auto fields = Object({{"item", std::move(item)}});
  auto required = Array({String("item")});
  if (!custom || input.GetBool("include_sample_schema")) {
    Set(&fields, "sample", custom ? SampleSchema() : Type("object"));
    required.array.push_back(String("sample"));
  }
  auto schema = Object({{"type", String("object")},
                        {"properties", std::move(fields)},
                        {"required", std::move(required)}});
  *out = Object({{"type", *input.Find("type")}, {"schema", std::move(schema)}});
  if (const auto* filters = input.Find("metadata")) {
    std::string param;
    auto valid = ValidateChatMetadata(*filters, &param);
    if (!valid) return Invalid("data_source_config." + param, valid.message());
    Set(out, "metadata", *filters);
  }
  return {};
}

ObjectReply Resource(const Json& record) {
  const auto* data = record.Find("data");
  const auto* eval = data ? data->Find("resource") : nullptr;
  if (!eval || data->GetInt("created_us") <= 0 ||
      data->GetInt("updated_us") < data->GetInt("created_us") ||
      eval->GetString("id") != record.GetString("id") ||
      !Checked(*eval, "eval").ok())
    return Corrupt();
  ObjectReply result;
  result.value = *eval;
  return result;
}

ObjectReply List(ObjectStore& objects, const StateAccess& access,
                 std::string_view raw) {
  auto query = ParseApplicationQuery(raw);
  if (!query.ok()) return query;
  auto checked = Checked(query.value, "query");
  if (!checked.ok()) return checked;
  const auto limit = query.value.GetInt("limit", 20);
  if (limit < 0) return Invalid("limit", "Expected a nonnegative page size");
  const char* timestamp = query.value.GetString("order_by") == "updated_at"
                              ? "updated_us"
                              : "created_us";
  const bool descending = query.value.GetString("order", "asc") == "desc";
  auto before = [descending](const SortKey& a, const SortKey& b) {
    return descending ? b < a : a < b;
  };
  std::set<SortKey, decltype(before)> keys(before);
  ObjectRequest read;
  read.kind = ObjectKind::kEval;
  read.scope = access.scope;
  SortKey cursor{};
  if (query.value.Has("after")) {
    read.id = query.value.GetString("after");
    if (!read.id.starts_with("eval_") || !ValidApplicationId(read.id))
      return Invalid("after", "Invalid eval cursor");
    auto row = objects.Execute(read, access.deadline, access.cancelled);
    if (row.http_status == 404) return Invalid("after", "Unknown eval cursor");
    if (!row.ok()) return row;
    cursor = {Field(row.value, "data").GetInt(timestamp), read.id};
    if (cursor.first <= 0) return Corrupt();
  }
  ObjectRequest scan;
  scan.kind = read.kind;
  scan.scope = read.scope;
  scan.operation = ObjectOperation::kList;
  scan.descending = false;
  scan.limit = 100;
  bool more = false;
  for (;;) {
    auto page = objects.Execute(scan, access.deadline, access.cancelled);
    if (!page.ok()) return page;
    const auto* data = page.value.GetArray("data");
    if (!data) return Corrupt();
    for (const auto& row : data->array) {
      const auto* stored = row.Find("data");
      SortKey key{stored ? stored->GetInt(timestamp) : 0, row.GetString("id")};
      if (key.first <= 0 || key.second.empty()) return Corrupt();
      if (cursor.first && !before(cursor, key)) continue;
      keys.emplace(std::move(key));
      if (keys.size() > static_cast<size_t>(limit)) {
        keys.erase(std::prev(keys.end()));
        more = true;
      }
    }
    if (!page.value.GetBool("has_more")) break;
    const auto next = page.value.GetString("last_id");
    if (next.empty() || next == scan.after) return Corrupt();
    scan.after = next;
  }
  auto data = Array();
  std::string first, last;
  size_t bytes = 0;
  for (const auto& key : keys) {
    read.id = key.second;
    auto row = objects.Execute(read, access.deadline, access.cancelled);
    if (row.http_status == 404) continue;
    if (!row.ok()) return row;
    auto result = Resource(row.value);
    if (!result.ok()) return result;
    const auto size = OutputJson(result.value).size();
    if (size > kMaxFrame - bytes && !first.empty()) {
      more = true;
      break;
    }
    if (size > kMaxFrame - bytes)
      return Error(413, "eval_record_too_large",
                   "Evaluation exceeds page budget");
    bytes += size;
    if (first.empty()) first = read.id;
    last = read.id;
    data.array.push_back(std::move(result.value));
  }
  ObjectReply result;
  result.value = Object({{"object", String("list")},
                         {"data", std::move(data)},
                         {"first_id", String(first)},
                         {"last_id", String(last)},
                         {"has_more", Boolean(more && !last.empty())}});
  return result;
}
}  // namespace

ObjectReply HandleEval(ObjectStore& objects, const StateAccess& access,
                       std::string_view method, std::string_view target,
                       const std::string& body, ResponsesJobs* jobs,
                       std::string_view report_base) {
  const auto route = EvalOperation(method, target);
  if (route.kind == EvalRouteKind::kNone)
    return Error(404, "route_not_found", "Unknown eval route");
  const auto split = target.find('?');
  const auto path = target.substr(0, split);
  const bool collection = path == "/v1/evals";
  if (!EvalMethodAllowed(method, target)) {
    auto error = Error(405, "method_not_allowed", "Method not allowed");
    error.value =
        Object({{"allow", String(std::string(EvalAllow(route.kind)))}});
    return error;
  }
  if (route.kind != EvalRouteKind::kCollection &&
      route.kind != EvalRouteKind::kEval) {
    const auto query = split == target.npos ? "" : target.substr(split + 1);
    ObjectReply reply;
    constexpr int kMaxCancelAttempts = 8;
    for (int attempt = 0; attempt < kMaxCancelAttempts; ++attempt) {
      reply = eval_detail::RunRequest(objects, access, route, method, query,
                                      body, jobs, report_base);
      // The dispatcher can finish sampling cleanup after the cancel commits.
      // Re-read the owned run when a known concurrent revision wins.
      if (route.kind != EvalRouteKind::kCancel || reply.commit_unknown ||
          reply.code != "object_revision_conflict")
        break;
    }
    if (reply.ok() && (route.kind == EvalRouteKind::kRun ||
                       route.kind == EvalRouteKind::kCancel ||
                       (route.kind == EvalRouteKind::kRuns && method == "POST")))
      eval_detail::ProjectRun(&reply.value, report_base);
    return reply;
  }
  if (method != "POST" && !body.empty())
    return Invalid("body", "Unexpected body");
  if (collection && method == "GET")
    return List(objects, access,
                split == target.npos ? "" : target.substr(split + 1));
  if (split != target.npos) return Invalid("query", "Unexpected query");
  Json input;
  if (method == "POST") {
    auto parsed = io::ParseJson(body, &input, true, kApplicationJsonLimits);
    if (!parsed) return Invalid("body", parsed.message());
    auto checked = Checked(input, collection ? "create" : "update");
    if (!checked.ok()) return checked;
    if (const auto* metadata = input.Find("metadata")) {
      std::string param;
      auto valid = ValidateChatMetadata(*metadata, &param);
      if (!valid) return Invalid(std::move(param), valid.message());
    }
  }
  const auto now = std::chrono::duration_cast<std::chrono::microseconds>(
                       std::chrono::system_clock::now().time_since_epoch())
                       .count();
  ObjectRequest request;
  request.scope = access.scope;
  request.kind = ObjectKind::kEval;
  Json resource, runs;
  int64_t created = now, updated = now;
  if (collection) {
    request.id = NewObjectId(request.kind);
    if (request.id.empty())
      return Error(503, "eval_id_unavailable", "Cannot allocate eval ID");
    request.ttl_seconds = 0;
    request.created_at = now / 1000000;
    Json source;
    auto ready = SourceConfig(*input.Find("data_source_config"), &source);
    if (!ready.ok()) return ready;
    resource =
        Object({{"id", String(request.id)},
                {"object", String("eval")},
                {"name", String(input.GetString("name", "Untitled eval"))},
                {"metadata", Object()},
                {"created_at", Number(request.created_at)},
                {"data_source_config", std::move(source)},
                {"testing_criteria", *input.Find("testing_criteria")}});
  } else {
    request.id = path.substr(10);
    auto row = objects.Execute(request, access.deadline, access.cancelled);
    if (!row.ok()) return row;
    auto prior = Resource(row.value);
    if (!prior.ok() || method == "GET") return prior;
    request.expected_revision = row.value.GetInt("revision");
    request.expires_at = row.value.GetInt("expires_at");
    if (method == "DELETE") {
      auto children =
          eval_detail::RemoveChildren(objects, access, &request, &row.value, jobs);
      if (!children.ok()) return children;
      request.operation = ObjectOperation::kErase;
      auto removed =
          objects.Execute(request, access.deadline, access.cancelled);
      if (removed.ok())
        removed.value = Object({{"object", String("eval.deleted")},
                                {"deleted", Boolean(true)},
                                {"eval_id", String(request.id)}});
      return removed;
    }
    const auto& data = Field(row.value, "data");
    if (const auto* ids = data.Find("run_ids")) runs = *ids;
    created = data.GetInt("created_us");
    updated = std::max<int64_t>(now, data.GetInt("updated_us") + 1);
    resource = std::move(prior.value);
    if (const auto* name = input.Find("name")) Set(&resource, "name", *name);
  }
  if (const auto* metadata = input.Find("metadata"))
    Set(&resource, "metadata", *metadata);
  if (!Checked(resource, "eval").ok()) return Corrupt();
  request.operation = ObjectOperation::kPut;
  request.data = Object({{"resource", resource},
                         {"created_us", Number(created)},
                         {"updated_us", Number(updated)}});
  if (!runs.IsNull()) Set(&request.data, "run_ids", std::move(runs));
  auto saved = objects.Execute(request, access.deadline, access.cancelled);
  if (saved.ok()) {
    saved.http_status = collection ? 201 : 200;
    saved.value = std::move(resource);
  }
  return saved;
}
}  // namespace q4t::server
