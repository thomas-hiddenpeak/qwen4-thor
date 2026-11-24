#include "assistant_runs_internal.h"

#include <algorithm>
#include <ctime>
#include <set>

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/responses_jobs.h"

namespace q4t::server::assistant_detail {
namespace {
using namespace object_detail;
ObjectReply Checked(const Json& value, AssistantShape shape) {
  ChatPlanError error;
  auto valid = ValidateAssistant(value, shape, &error);
  return valid ? ObjectReply{} : Invalid(valid.message(), error.param);
}
void Erase(Json* value, std::string_view key) {
  std::erase_if(value->object,
                [&](const auto& field) { return field.first == key; });
}
ObjectReply Submit(ResponsesJobs* jobs, const std::string& thread,
                   const Json& resource, ObjectRequest* write,
                   std::vector<ObjectRequest> writes, Json* entry,
                   bool continuation,
                   std::unique_ptr<ResponsesExecution> execution, int fd,
                   std::string* admitted) {
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!jobs || !http || !http->Client().Allowed())
    return Error(503, "background_unavailable", "Run executor unavailable");
  auto& ids = Field(*entry, "response_ids");
  ids.array.push_back(String(execution->Id()));
  auto& runs = Field(write->data, "runs");
  if (runs.IsNull()) runs = Array();
  if (continuation)
    runs.array.back() = *entry;
  else
    runs.array.push_back(*entry);
  for (auto& participant : writes)
    if (participant.kind == ObjectKind::kThread) participant = *write;
  auto linked = execution->BindAssistantRun(thread, resource.GetString("id"),
                                            std::move(writes));
  if (!linked.ok()) return linked;
  if (!execution->BindRateDomain(http->RateDomain()))
    return Error(503, "response_rate_domain_changed",
                 "Run accounting origin changed");
  *admitted = execution->Id();
  auto posted = jobs->Submit(
      std::move(execution), http->Client(),
      [&](ClientLease& lease) { return http->TransferClientLease(lease); },
      http->TakeBatchRate());
  if (posted.ok()) posted.value = resource;
  if (posted.commit_unknown || posted.code == "background_admission_lost") {
    posted.commit_unknown = true;
    posted.value = Object({{"id", *resource.Find("id")}});
  }
  return posted;
}
}  // namespace

ObjectReply CreateRun(ObjectStore& objects, ResponsesJobs* jobs, int fd,
                      const StateAccess& access, const ChatPlanConfig& config,
                      ObjectRequest write, const Json& request, bool new_thread,
                      bool ingestion, std::string* admitted) {
  if (!jobs)
    return Error(503, "background_unavailable", "Run executor unavailable");
  auto valid = Checked(request, new_thread ? AssistantShape::kThreadRunCreate
                                           : AssistantShape::kRunCreate);
  if (!valid.ok()) return valid;
  valid = ObserveLastRun(objects, access, &write.data, true);
  if (!valid.ok()) return valid;
  ObjectRequest assistant;
  assistant.kind = ObjectKind::kAssistant;
  assistant.scope = access.scope;
  assistant.id = request.GetString("assistant_id");
  if (!IsAssistantTarget("/v1/assistants/" + assistant.id))
    return Invalid("Invalid Assistant ID", "assistant_id");
  auto stored = objects.Execute(assistant, access.deadline, access.cancelled);
  if (!stored.ok()) return stored;
  const BinaryAccess binary{access.scope, access.deadline, access.cancelled};
  auto& thread = Field(write.data, "thread");
  Attachments attachments;
  const auto* seed = request.Find("thread");
  if (seed) {
    valid = Checked(*seed, AssistantShape::kThreadCreate);
    if (!valid.ok()) return valid;
    for (const auto* field : {"tool_resources", "metadata"})
      if (const auto* v = seed->Find(field)) Set(&thread, field, *v);
  }
  if (const auto* resources = request.Find("tool_resources")) {
    // A combined create can override the assistant's resources for this run.
    auto bound = *resources;
    Json inline_store;
    std::string store_id;
    auto check = BindResources(objects, binary, &bound, false, ingestion,
                               &inline_store, &store_id);
    if (!check.ok()) return check;
    if (!store_id.empty())
      return Invalid("Use an existing store for run resources",
                     "tool_resources");
    Set(&Field(Field(stored.value, "data"), "assistant"), "tool_resources",
        bound);
  }
  const auto* extra = new_thread ? (seed ? seed->GetArray("messages") : nullptr)
                                 : request.GetArray("additional_messages");
  if (extra) {
    for (const auto& input : extra->array) {
      Json row;
      auto result =
          CreateMessage(objects, binary, write.id, input, &attachments, &row);
      if (!result.ok()) return result;
      Field(write.data, "messages").array.push_back(std::move(row));
    }
  }
  write.operation = ObjectOperation::kPut;
  std::vector<ObjectRequest> mutations;
  auto bound = SaveThread(objects, binary, new_thread,
                          seed && seed->Has("tool_resources"), ingestion,
                          attachments, &write, &mutations);
  if (!bound.ok()) return bound;
  const auto now = std::time(nullptr);
  Json run, native;
  auto settings =
      PrepareRunSettings(stored.value, write.data, request, config,
                         detail::NewRequestId("run_"), now, &run, &native);
  if (!settings.ok()) return settings;
  auto execution = std::make_unique<ResponsesExecution>(objects, jobs, true);
  auto ready = execution->Prepare(native, config, access, now, true);
  if (!ready.ok()) return ready;
  Erase(&native, "input");
  auto entry = Object({{"run", run},
                       {"native_settings", std::move(native)},
                       {"response_ids", Array()},
                       {"submitted", Object()}});
  return Submit(jobs, write.id, run, &write, std::move(mutations), &entry,
                false, std::move(execution), fd, admitted);
}

ObjectReply SubmitOutputs(ObjectStore& objects, ResponsesJobs* jobs, int fd,
                          const StateAccess& access,
                          const ChatPlanConfig& config, ObjectRequest write,
                          const Json& request, const std::string& run_id,
                          std::string* admitted) {
  if (!jobs)
    return Error(503, "background_unavailable", "Run executor unavailable");
  auto valid = Checked(request, AssistantShape::kRunSubmit);
  if (!valid.ok()) return valid;
  const auto* runs = write.data.GetArray("runs");
  if (!runs || runs->array.empty() ||
      runs->array.back().Find("run")->GetString("id") != run_id)
    return Error(404, "run_not_found", "Active run not found in this thread");
  auto entry = runs->array.back();
  RunView view;
  auto observed = ProjectRun(objects, access, entry, &view);
  if (!observed.ok()) return observed;
  if (view.run.GetString("status") != "requires_action")
    return Error(409, "run_not_waiting", "Run is not waiting for tool outputs");
  const auto* calls = view.run.Find("required_action")
                          ->Find("submit_tool_outputs")
                          ->GetArray("tool_calls");
  std::set<std::string> pending;
  for (const auto& call : calls->array) pending.insert(call.GetString("id"));
  auto input = Array();
  for (const auto& output : request.GetArray("tool_outputs")->array) {
    const auto call = output.GetString("tool_call_id");
    const auto* text = output.Find("output");
    if (!text || !text->IsString() || !pending.erase(call))
      return Invalid("Expected one output for each pending tool call",
                     "tool_outputs");
    Set(&Field(entry, "submitted"), call, *text);
    input.array.push_back(Object({{"type", String("function_call_output")},
                                  {"call_id", String(call)},
                                  {"output", *text}}));
  }
  if (!pending.empty())
    return Invalid("Submit all pending tool calls together", "tool_outputs");
  auto native = *entry.Find("native_settings");
  const auto& ids = entry.Find("response_ids")->array;
  // A submitted result satisfies the original required or named choice.
  // Keep the published Run settings and allow a final answer on this leg.
  Set(&native, "tool_choice", String("auto"));
  Set(&native, "previous_response_id", ids.back());
  Set(&native, "input", std::move(input));
  const auto max_tokens = view.run.GetInt("max_completion_tokens");
  if (max_tokens > 0) {
    int64_t remaining = max_tokens;
    for (const auto& id : ids) {
      auto record = ResponsesState(objects).ReadRecord(id.str, access);
      if (!record.ok()) return record;
      const auto* usage = record.value.Find("response")->Find("usage");
      if (usage) remaining -= usage->GetInt("output_tokens");
    }
    if (remaining <= 0)
      return Error(409, "run_budget_exhausted",
                   "Completion token budget exhausted");
    Set(&native, "max_output_tokens", Number(remaining));
  }
  auto execution = std::make_unique<ResponsesExecution>(objects, jobs, true);
  auto ready =
      execution->Prepare(native, config, access, std::time(nullptr), true);
  if (!ready.ok()) return ready;
  if (!entry.Has("settled_calls")) Set(&entry, "settled_calls", Object());
  const auto settlement = Object({{"response_id", String(execution->Id())},
                                  {"at", Number(std::time(nullptr))}});
  for (const auto& row : request.GetArray("tool_outputs")->array)
    Set(&Field(entry, "settled_calls"), row.GetString("tool_call_id"),
        settlement);
  write.operation = ObjectOperation::kPut;
  Set(&view.run, "status", String("queued"));
  Set(&view.run, "required_action", {});
  return Submit(jobs, write.id, view.run, &write, {write}, &entry, true,
                std::move(execution), fd, admitted);
}
}  // namespace q4t::server::assistant_detail
