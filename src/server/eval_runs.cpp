#include "eval_run_state.h"
#include "eval_sampling.h"

#include <algorithm>
#include <ctime>

#include "q4t/server/application_query.h"
#include "q4t/server/chat_storage.h"
#include "q4t/server/files.h"
#include "q4t/server/request_json.h"

namespace q4t::server::eval_detail {
namespace {
using namespace object_detail;
ObjectReply Get(ObjectStore& objects, const StateAccess& access,
                ObjectKind kind, std::string_view id) {
  return objects.Execute(Read(kind, access, id), access.deadline,
                         access.cancelled);
}
// A string in a standard template message matches both branches of upstream
// oneOf. The equivalent typed text part selects EvalItem unambiguously. Keep
// the original run request internally so replay uses exactly the submitted
// input.
void PublicRunTemplate(Json* run) {
  auto* source = run->Find("data_source");
  if (!source || !source->Has("input_messages")) return;
  auto& spec = Field(Field(*run, "data_source"), "input_messages");
  if (spec.GetString("type") != "template" || !spec.GetArray("template"))
    return;
  for (auto& message : Field(spec, "template").array) {
    const auto role = message.GetString("role");
    const auto* content = message.Find("content");
    if (content && content->IsString() &&
        (role == "user" || role == "assistant" || role == "system" ||
         role == "developer"))
      Set(&message, "content",
          Object({{"type", String("input_text")}, {"text", *content}}));
  }
}

ObjectReply Public(const Json& record, const EvalRoute& route, bool output) {
  const auto* data = record.Find("data");
  const auto* item = data ? data->Find("resource") : nullptr;
  if (!item || item->GetString("id") != record.GetString("id"))
    return InvalidRecord();
  if (item->GetString("eval_id") != route.eval ||
      (output && item->GetString("run_id") != route.run) ||
      data->GetBool("deleting"))
    return NotFound();
  if (!Shape(*item, output ? "output_item" : "run").ok())
    return InvalidRecord();
  ObjectReply result;
  result.value = *item;
  if (!output) PublicRunTemplate(&result.value);
  return result;
}
ObjectReply Create(ObjectStore& objects, const StateAccess& access,
                   const EvalRoute& route, const Json& definition,
                   const Json& input) {
  auto checked = Shape(input, "run_create");
  if (!checked.ok()) return checked;
  const auto* metadata = input.Find("metadata");
  if (metadata) {
    std::string field;
    auto valid = ValidateChatMetadata(*metadata, &field);
    if (!valid) return BadInput(field, valid.message());
  }
  auto parent = Update(ObjectKind::kEval, access, definition);
  const auto* eval = parent.data.Find("resource");
  const auto* source = input.Find("data_source");
  if (!eval || !source) return InvalidRecord();
  const auto* config = eval->Find("data_source_config");
  const auto* criteria = eval->Find("testing_criteria");
  if (!config || !config->Find("schema") || !criteria || !criteria->IsArray())
    return InvalidRecord();
  auto run =
      Read(ObjectKind::kEvalRun, access, NewObjectId(ObjectKind::kEvalRun));
  if (run.id.empty())
    return Error(503, "eval_id_unavailable", "Cannot allocate run ID");
  run.operation = ObjectOperation::kPut;
  run.ttl_seconds = 0;
  run.created_at = std::time(nullptr);
  const auto* model = source->Find("model");
  const auto* name = input.Find("name");
  auto resource =
      Object({{"id", String(run.id)},
              {"object", String("eval.run")},
              {"eval_id", String(std::string(route.eval))},
              {"status", String("queued")},
              {"name", name ? *name : Json()},
              {"model", model ? *model : Json()},
              {"created_at", Number(run.created_at)},
              {"report_url", String("/v1/evals/" + parent.id + "/runs/" +
                                    run.id + "/output_items")},
              {"result_counts", Object({{"total", Number(0)},
                                        {"errored", Number(0)},
                                        {"passed", Number(0)},
                                        {"failed", Number(0)}})},
              {"metadata", metadata ? *metadata : Object()},
              {"data_source", *source},
              {"per_model_usage", Json()},
              {"per_testing_criteria_results", Json()},
              {"error", Json()}});
  auto formatted = Shape(resource, "run");
  if (!formatted.ok())
    return Error(503, "eval_record_invalid",
                 "Invalid run resource at " +
                     formatted.value.GetString("param") + ": " +
                     formatted.message);
  run.data = Object({{"format", String("q4t.eval_run.v1")},
                     {"resource", resource},
                     {"criteria", *criteria},
                     {"schema", *config->Find("schema")},
                     {"output_ids", Array()},
                     {"row", Number(0)},
                     {"chunk", Number(0)},
                     {"offset", Number(0)}});
  auto& ids = Field(parent.data, "run_ids");
  if (ids.IsNull()) ids = Array();
  if (!ids.IsArray()) return InvalidRecord();
  ids.array.push_back(String(run.id));
  std::vector<ObjectRequest> writes;
  BinaryAccess binary_access{access.scope, access.deadline, access.cancelled};
  BinaryRead pin;
  const auto* origin = source->Find("source");
  if (origin && origin->GetString("type") == "file_id") {
    auto opened =
        Files(objects).Open(binary_access, origin->GetString("id"), &pin);
    if (!opened.ok()) return opened;
    // The issuing File's pin and the new persistent run root are committed
    // together so deletion of the original File cannot strand queued input.
    const auto blob = BinaryManifestJson(pin.manifest);
    Set(&run.data, "blob", blob);
    auto lease = Read(ObjectKind::kToolRun, access, pin.id);
    lease.operation = ObjectOperation::kPut;
    lease.expected_revision = 1;
    lease.expires_at = pin.expires;
    lease.mutate_before = pin.expires;
    lease.data =
        Object({{"format", String("q4t.binary_read.v1")}, {"blob", blob}});
    writes.push_back(std::move(lease));
  }
  writes.push_back(std::move(parent));
  writes.push_back(std::move(run));
  auto stored = objects.ExecuteBatch(writes, access.deadline, access.cancelled);
  if (!pin.id.empty() && !stored.commit_unknown) {
    auto closed = BinaryStore(objects).Close(binary_access, &pin);
    if (!closed.ok()) objects.RecordAuxiliaryCleanupFailure();
  }
  if (stored.ok()) {
    stored.value = std::move(resource);
    PublicRunTemplate(&stored.value);
    stored.http_status = 201;
  } else if (stored.commit_unknown) {
    stored.value = Object({{"id", *resource.Find("id")}});
  }
  return stored;
}
ObjectReply List(ObjectStore& objects, const StateAccess& access,
                 const EvalRoute& route, const Json& parent,
                 std::string_view raw, bool output) {
  auto query = ParseApplicationQuery(raw);
  if (!query.ok()) return query;
  auto valid = Shape(query.value, output ? "output_query" : "run_query");
  if (!valid.ok()) return valid;
  const auto limit = query.value.GetInt("limit", 20);
  if (limit < 0) return BadInput("limit", "Expected a nonnegative page size");
  const auto* state = parent.Find("data");
  if (!state) return InvalidRecord();
  const auto* ids = state->Find(output ? "output_ids" : "run_ids");
  if (ids && !ids->IsArray()) return InvalidRecord();
  const auto count = ids ? ids->array.size() : 0;
  const bool desc = query.value.GetString("order", "asc") == "desc";
  const auto after = query.value.GetString("after");
  const auto filter = query.value.GetString("status");
  bool found = after.empty(), more = false;
  auto items = Array();
  size_t bytes = 0;
  for (size_t i = 0; i < count; ++i) {
    const auto& id = ids->array[desc ? count - 1 - i : i];
    if (!id.IsString()) return InvalidRecord();
    if (!found) {
      found = id.str == after;
      continue;
    }
    auto row =
        Get(objects, access,
            output ? ObjectKind::kEvalOutput : ObjectKind::kEvalRun, id.str);
    if (row.http_status == 404) continue;
    if (!row.ok()) return row;
    auto item = Public(row.value, route, output);
    if (item.http_status == 404) continue;
    if (!item.ok()) return item;
    if (!filter.empty() && item.value.GetString("status") != filter) continue;
    auto size = OutputJson(item.value).size();
    if (items.array.size() >= static_cast<size_t>(limit) ||
        size > kMaxFrame - bytes) {
      if (items.array.empty() && limit > 0)
        return Error(413, "eval_record_too_large",
                     "Evaluation exceeds page budget");
      more = !items.array.empty();
      break;
    }
    bytes += size;
    items.array.push_back(std::move(item.value));
  }
  if (!found) return BadInput("after", "Unknown evaluation cursor");
  const auto first =
      items.array.empty() ? String("") : *items.array.front().Find("id");
  const auto last =
      items.array.empty() ? String("") : *items.array.back().Find("id");
  ObjectReply result;
  result.value = Object({{"object", String("list")},
                         {"data", std::move(items)},
                         {"first_id", first},
                         {"last_id", last},
                         {"has_more", Boolean(more)}});
  return result;
}

ObjectReply DeleteRun(ObjectStore& objects, const StateAccess& access,
                      const Json& definition, const Json& record,
                      ResponsesJobs* jobs) {
  auto parent = Update(ObjectKind::kEval, access, definition);
  auto run = Update(ObjectKind::kEvalRun, access, record);
  auto* children = run.data.Find("output_ids");
  const auto* resource = run.data.Find("resource");
  if (!children || !children->IsArray() || !resource) return InvalidRecord();
  if (resource->GetString("eval_id") != parent.id) return NotFound();
  if (!run.data.GetBool("deleting") && run.data.Has("pending_response")) {
    Set(&run.data, "deleting", Boolean(true));
    auto fenced = objects.Execute(run, access.deadline, access.cancelled);
    if (!fenced.ok()) return fenced;
    ++run.expected_revision;
  }
  auto released = ReleaseSampling(objects, access, jobs, run);
  if (!released.ok()) return released;
  auto& runs = Field(parent.data, "run_ids");
  if (!runs.IsArray()) return InvalidRecord();
  auto it = std::find_if(
      runs.array.begin(), runs.array.end(),
      [&](const Json& id) { return id.IsString() && id.str == run.id; });
  if (it == runs.array.end()) return NotFound();
  runs.array.erase(it);
  Set(&run.data, "deleting", Boolean(true));
  auto& ids = Field(run.data, "output_ids").array;
  for (;;) {
    std::vector<ObjectRequest> writes;
    while (!ids.empty() && writes.size() < 128) {
      auto erase = Read(ObjectKind::kEvalOutput, access, ids.back().str);
      erase.operation = ObjectOperation::kErase;
      writes.push_back(std::move(erase));
      ids.pop_back();
    }
    if (ids.empty()) {
      run.operation = ObjectOperation::kErase;
      writes.push_back(parent);
    }
    writes.push_back(run);
    auto result =
        objects.ExecuteBatch(writes, access.deadline, access.cancelled);
    if (!result.ok()) return result;
    if (ids.empty()) {
      result.value = Object({{"object", String("eval.run.deleted")},
                             {"run_id", String(run.id)},
                             {"deleted", Boolean(true)}});
      return result;
    }
    ++run.expected_revision;
  }
}
}  // namespace

ObjectReply ResumeRunDeletion(ObjectStore& objects, const StateAccess& access,
                              const Json& record, ResponsesJobs* jobs) {
  const auto* data = record.Find("data");
  const auto* resource = data ? data->Find("resource") : nullptr;
  if (!resource || !data->GetBool("deleting")) return InvalidRecord();
  auto parent =
      Get(objects, access, ObjectKind::kEval, resource->GetString("eval_id"));
  return parent.ok() ? DeleteRun(objects, access, parent.value, record, jobs)
                     : parent;
}

ObjectReply RemoveChildren(ObjectStore& objects, const StateAccess& access,
                           ObjectRequest* parent, Json* record,
                           ResponsesJobs* jobs) {
  for (;;) {
    const auto* data = record->Find("data");
    if (!data) return InvalidRecord();
    const auto* ids = data->Find("run_ids");
    if (!ids || (ids->IsArray() && ids->array.empty())) return {};
    if (!ids->IsArray() || !ids->array.back().IsString())
      return InvalidRecord();
    auto row =
        Get(objects, access, ObjectKind::kEvalRun, ids->array.back().str);
    if (!row.ok()) return row;
    auto deleted = DeleteRun(objects, access, *record, row.value, jobs);
    if (!deleted.ok()) return deleted;
    auto current = Get(objects, access, ObjectKind::kEval, parent->id);
    if (!current.ok()) return current;
    *record = std::move(current.value);
    parent->expected_revision = record->GetInt("revision");
  }
}
ObjectReply RunRequest(ObjectStore& objects, const StateAccess& access,
                       const EvalRoute& route, std::string_view method,
                       std::string_view query, const std::string& body,
                       ResponsesJobs* jobs) {
  using Kind = EvalRouteKind;
  const bool list = method == "GET" &&
                    (route.kind == Kind::kRuns || route.kind == Kind::kOutputs);
  if (!list && !query.empty()) return BadInput("query", "Unexpected query");
  if (method != "POST" && !body.empty())
    return BadInput("body", "Unexpected body");
  auto parent = Get(objects, access, ObjectKind::kEval, route.eval);
  if (!parent.ok()) return parent;
  if (route.kind == Kind::kRuns) {
    if (list) return List(objects, access, route, parent.value, query, false);
    Json input;
    auto parsed = io::ParseJson(body, &input, true, kApplicationJsonLimits);
    if (!parsed) return BadInput("body", parsed.message());
    return Create(objects, access, route, parent.value, input);
  }
  auto run = Get(objects, access, ObjectKind::kEvalRun, route.run);
  if (!run.ok()) return run;
  if (method == "DELETE")
    return DeleteRun(objects, access, parent.value, run.value, jobs);
  auto result = Public(run.value, route, false);
  if (!result.ok()) return result;
  if (route.kind == Kind::kRun) return result;
  if (route.kind == Kind::kOutputs)
    return List(objects, access, route, run.value, query, true);
  if (route.kind == Kind::kOutput) {
    auto row = Get(objects, access, ObjectKind::kEvalOutput, route.output);
    return row.ok() ? Public(row.value, route, true) : row;
  }
  if (!body.empty() && body != "{}") {
    Json parsed;
    if (!io::ParseJson(body, &parsed, true, kApplicationJsonLimits) ||
        !parsed.IsObject() || !parsed.object.empty())
      return BadInput("body", "Unexpected cancel body");
  }
  const auto status = result.value.GetString("status");
  if (status != "canceled" && status != "queued" && status != "in_progress")
    return Error(409, "eval_run_terminal", "Evaluation has already finished");
  auto write = Update(ObjectKind::kEvalRun, access, run.value);
  if (status != "canceled") {
    Set(&result.value, "status", String("canceled"));
    Set(&Field(write.data, "resource"), "status", String("canceled"));
    auto saved = objects.Execute(write, access.deadline, access.cancelled);
    if (!saved.ok()) return saved;
    ++write.expected_revision;
  }
  auto released = ReleaseSampling(objects, access, jobs, write);
  return released.ok() ? result : released;
}
}  // namespace q4t::server::eval_detail
