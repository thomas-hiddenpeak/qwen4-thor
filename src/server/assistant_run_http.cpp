#include "assistant_runs_internal.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/http_response.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace {
using namespace assistant_detail;
using namespace object_detail;
using Kind = AssistantRunRouteKind;

ObjectReply Result(Json value) {
  ObjectReply reply;
  reply.value = std::move(value);
  return reply;
}

ObjectReply CheckInput(const Json& input, AssistantShape shape) {
  ChatPlanError error;
  auto valid = ValidateAssistant(input, shape, &error);
  return valid ? ObjectReply{} : Invalid(valid.message(), error.param);
}

void SelectSearchContent(Json* steps, bool include) {
  if (include) return;
  for (auto& step : steps->array) {
    const auto* details = step.Find("step_details");
    if (!details || !details->GetArray("tool_calls")) continue;
    for (auto& call : Field(Field(step, "step_details"), "tool_calls").array) {
      if (call.GetString("type") != "file_search") continue;
      auto& search = Field(call, "file_search");
      if (search.Has("results"))
        for (auto& row : Field(search, "results").array)
          std::erase_if(row.object,
                        [](const auto& f) { return f.first == "content"; });
    }
  }
}

ObjectReply Dispatch(ObjectStore& objects, ResponsesJobs* jobs, int fd,
                     std::string_view method, std::string_view target,
                     const std::string& body, const StateAccess& access,
                     const ChatPlanConfig& config, bool ingestion, bool* stream,
                     std::string* admitted, bool* include) {
  const auto route = ParseAssistantRunRoute(target);
  if (!AssistantRunMethodAllowed(method, target))
    return Error(405, "method_not_allowed", "Method not allowed");
  const auto split = target.find('?');
  auto query = ParseApplicationQuery(
      split == target.npos ? "" : target.substr(split + 1));
  if (!query.ok()) return query;
  const auto* fields = query.value.GetArray("include");
  *include = fields && !fields->array.empty();
  const bool list = method == "GET" && route.kind == Kind::kRuns;
  if (list || route.kind == Kind::kStep || route.kind == Kind::kSteps ||
      (method == "POST" && route.kind == Kind::kRuns)) {
    const auto shape = list ? AssistantShape::kRunListQuery
                       : route.kind == Kind::kSteps
                           ? AssistantShape::kStepListQuery
                           : AssistantShape::kStepQuery;
    auto valid = CheckInput(query.value, shape);
    if (!valid.ok()) return valid;
  } else if (split != target.npos)
    return Invalid("Unexpected query", "query");
  Json request;
  if (method == "POST" && route.kind != Kind::kCancel) {
    auto valid = io::ParseJson(body, &request, true, kApplicationJsonLimits);
    if (!valid) return Invalid(valid.message(), "body");
  } else if (!body.empty()) {
    return Invalid("Unexpected body", "body");
  }
  ObjectRequest write;
  auto loaded = LoadThreadDraft(objects, access, route.thread, &write);
  if (!loaded.ok()) return loaded;
  if (method == "POST" &&
      (route.kind == Kind::kCreateThread || route.kind == Kind::kRuns)) {
    *stream = request.GetBool("stream");
    return CreateRun(objects, jobs, fd, access, config, std::move(write),
                     request, route.kind == Kind::kCreateThread, ingestion,
                     admitted);
  }
  const auto* runs = write.data.GetArray("runs");
  if (list) {
    auto rows = Array();
    if (runs) {
      for (const auto& entry : runs->array) {
        if (!entry.Has("run"))
          return Error(503, "assistant_run_invalid", "Invalid stored run");
        rows.array.push_back(*entry.Find("run"));
      }
    }
    auto page = AssistantListResult(PageAssistantRows(rows, query.value));
    if (!page.ok()) return page;
    for (auto& row : Field(page.value, "data").array) {
      const auto id = row.GetString("id");
      auto entry = std::ranges::find_if(runs->array, [&](const Json& r) {
        return r.Find("run")->GetString("id") == id;
      });
      RunView view;
      auto projected = ProjectRun(objects, access, *entry, &view);
      if (!projected.ok()) return projected;
      row = std::move(view.run);
    }
    return page;
  }
  if (!runs) return Error(404, "run_not_found", "Run not found in this thread");
  auto& entries = Field(write.data, "runs").array;
  const auto it = std::ranges::find_if(entries, [&](const Json& entry) {
    const auto* run = entry.Find("run");
    return run && run->GetString("id") == route.run;
  });
  if (it == entries.end())
    return Error(404, "run_not_found", "Run not found in this thread");
  if (route.kind == Kind::kSubmit) {
    *stream = request.GetBool("stream");
    return SubmitOutputs(objects, jobs, fd, access, config, std::move(write),
                         request, std::string(route.run), admitted);
  }
  RunView view;
  auto observed = ProjectRun(objects, access, *it, &view);
  if (!observed.ok()) return observed;
  if (route.kind == Kind::kRun && method == "GET")
    return Result(std::move(view.run));
  if (route.kind == Kind::kStep || route.kind == Kind::kSteps) {
    const auto* fields = query.value.GetArray("include");
    SelectSearchContent(&view.steps, fields && !fields->array.empty());
    if (route.kind == Kind::kSteps)
      return AssistantListResult(PageAssistantRows(view.steps, query.value));
    for (auto& row : view.steps.array)
      if (row.GetString("id") == route.step) return Result(std::move(row));
    return Error(404, "run_step_not_found", "Step not found in this run");
  }
  if (route.kind == Kind::kCancel) {
    if (!ActiveRun(view.run)) return Result(std::move(view.run));
    if (!jobs)
      return Error(503, "background_unavailable", "Run executor unavailable");
    auto cancelled =
        jobs->Cancel(it->GetArray("response_ids")->array.back().str, access);
    if (!cancelled.ok()) return cancelled;
    auto current = ProjectRun(objects, access, *it, &view);
    if (!current.ok()) return current;
    if (view.run.GetString("status") != "requires_action")
      return Result(std::move(view.run));
    Set(&*it, "cancelled_at", Number(std::time(nullptr)));
  } else {
    auto valid = CheckInput(request, AssistantShape::kRunUpdate);
    if (!valid.ok()) return valid;
    if (const auto* metadata = request.Find("metadata"))
      Set(&Field(*it, "run"), "metadata", *metadata);
  }
  auto updated = ProjectRun(objects, access, *it, &view);
  if (!updated.ok()) return updated;
  write.operation = ObjectOperation::kPut;
  auto result = objects.Execute(write, access.deadline, access.cancelled);
  if (result.ok()) result.value = std::move(view.run);
  if (result.commit_unknown)
    result.value = Object({{"id", String(std::string(route.run))}});
  return result;
}
}  // namespace

void ServeAssistantRun(int fd, ObjectStore& objects, ResponsesJobs* jobs,
                       std::string_view method, std::string_view path,
                       const std::string& body, const StateAccess& access,
                       const ChatPlanConfig& config, bool ingestion) {
  bool stream = false, include = false;
  std::string admitted;
  auto reply = Dispatch(objects, jobs, fd, method, path, body, access, config,
                        ingestion, &stream, &admitted, &include);
  if (!reply.ok()) {
    detail::SendError(fd, reply.http_status, reply.ClientMessage(),
                      {access.deadline}, reply.ClientCode(),
                      reply.value.GetString("param"),
                      reply.value.GetString("allow"));
    return;
  }
  if (stream) {
    StreamAssistantRun(fd, objects, access, reply.value,
                       ParseAssistantRunRoute(path).kind, admitted, include);
    return;
  }
  detail::SendSimple(fd, 200, "OK", OutputJson(reply.value), "application/json",
                     {access.deadline});
}
}  // namespace q4t::server
