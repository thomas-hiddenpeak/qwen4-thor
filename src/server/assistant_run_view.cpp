#include "assistant_runs_internal.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"

namespace q4t::server::assistant_detail {
namespace {
using namespace object_detail;
Json At(int64_t at) { return at ? Number(at) : Json(); }
ObjectReply CorruptRun() {
  return Error(503, "assistant_run_invalid", "Stored run is inconsistent");
}
Json Usage(int64_t input, int64_t output) {
  return Object({{"prompt_tokens", Number(input)},
                 {"completion_tokens", Number(output)},
                 {"total_tokens", Number(input + output)}});
}

Json PublicMessage(const Json& item, const Json& run, int64_t created,
                   int64_t ended) {
  auto content = Array();
  for (const auto& part : item.GetArray("content")->array) {
    if (part.GetString("type") == "refusal") {
      content.array.push_back(Object(
          {{"type", String("refusal")}, {"refusal", *part.Find("refusal")}}));
    } else if (part.GetString("type") == "output_text") {
      auto annotations = Array();
      if (const auto* refs = part.GetArray("annotations")) {
        for (const auto& ref : refs->array) {
          if (ref.GetString("type") != "file_citation") continue;
          const auto pos = Number(ref.GetInt("index"));
          annotations.array.push_back(
              Object({{"type", String("file_citation")},
                      {"text", String("")},
                      {"start_index", pos},
                      {"end_index", pos},
                      {"file_citation",
                       Object({{"file_id", *ref.Find("file_id")}})}}));
        }
      }
      content.array.push_back(Object(
          {{"type", String("text")},
           {"text", Object({{"value", *part.Find("text")},
                            {"annotations", std::move(annotations)}})}}));
    }
  }
  const auto status = item.GetString("status", "in_progress");
  const bool done = status == "completed", partial = status == "incomplete";
  return Object(
      {{"id", *item.Find("id")},
       {"object", String("thread.message")},
       {"created_at", Number(created)},
       {"thread_id", *run.Find("thread_id")},
       {"assistant_id", *run.Find("assistant_id")},
       {"run_id", *run.Find("id")},
       {"role", String("assistant")},
       {"status", String(status)},
       {"content", std::move(content)},
       {"attachments", Array()},
       {"metadata", Object()},
       {"completed_at", done ? At(ended) : Json()},
       {"incomplete_at", partial ? At(ended) : Json()},
       {"incomplete_details",
        partial ? Object({{"reason", String("max_tokens")}}) : Json()}});
}

Json Step(const Json& run, const Json& item, std::string type, Json detail,
          std::string status, int64_t created, int64_t ended) {
  const auto step_id = "step_" + item.GetString("id");
  return Object({{"id", String(step_id)},
                 {"object", String("thread.run.step")},
                 {"thread_id", *run.Find("thread_id")},
                 {"assistant_id", *run.Find("assistant_id")},
                 {"run_id", *run.Find("id")},
                 {"type", String(std::move(type))},
                 {"step_details", std::move(detail)},
                 {"status", String(status)},
                 {"created_at", Number(created)},
                 {"expired_at", status == "expired" ? At(ended) : Json()},
                 {"cancelled_at", status == "cancelled" ? At(ended) : Json()},
                 {"failed_at", status == "failed" ? At(ended) : Json()},
                 {"completed_at", status == "completed" ? At(ended) : Json()},
                 {"last_error", {}},
                 {"metadata", Object()},
                 {"usage", {}}});
}

Json SearchDetails(const Json& item) {
  auto details = Object();
  const auto* results = item.GetArray("results");
  if (!results) return details;
  auto rows = Array();
  for (const auto& found : results->array) {
    auto row = Object();
    for (const auto* key : {"file_id", "score"})
      if (const auto* v = found.Find(key)) Set(&row, key, *v);
    if (const auto* name = found.Find("filename"))
      Set(&row, "file_name", *name);
    if (const auto* text = found.Find("text"))
      Set(&row, "content",
          Array({Object({{"type", String("text")}, {"text", *text}})}));
    rows.array.push_back(std::move(row));
  }
  Set(&details, "results", std::move(rows));
  return details;
}
}  // namespace

bool ActiveRun(const Json& run) {
  const auto status = run.GetString("status");
  return status == "in_progress" || status == "queued" ||
         status == "requires_action" || status == "cancelling";
}

ObjectReply ProjectRun(ObjectStore& objects, const StateAccess& access,
                       const Json& entry, RunView* view) {
  const auto* base = entry.Find("run");
  const auto* ids = entry.GetArray("response_ids");
  if (!base || !ids || ids->array.empty()) return CorruptRun();
  *view = {*base, Array(), Array()};
  auto& run = view->run;
  auto calls = Array();
  int64_t input = 0, output = 0, finished = 0;
  bool has_usage = false;
  for (const auto& id : ids->array) {
    auto record = ResponsesState(objects).ReadRecord(id.str, access);
    if (!record.ok()) return record;
    const auto& data = record.value;
    const auto* response = data.Find("response");
    if (!response ||
        data.GetString("assistant_thread_id") != run.GetString("thread_id") ||
        data.GetString("assistant_run_id") != run.GetString("id"))
      return CorruptRun();
    const auto created = response->GetInt("created_at");
    finished = data.GetInt("assistant_finished_at");
    if (run.Find("started_at")->IsNull() && data.GetInt("assistant_started_at"))
      Set(&run, "started_at", Number(data.GetInt("assistant_started_at")));
    const auto stage = response->GetString("status");
    Set(&run, "status", String(stage));
    if (const auto* usage = response->Find("usage");
        usage && usage->IsObject()) {
      input += usage->GetInt("input_tokens");
      output += usage->GetInt("output_tokens");
      has_usage = true;
    }
    calls = Array();
    for (const auto& item : response->GetArray("output")->array) {
      const auto type = item.GetString("type");
      auto state = item.GetString("status", "in_progress");
      if (state != "completed")
        state =
            stage == "failed" || stage == "cancelled" ? stage : "in_progress";
      if (type == "message") {
        if (!item.Has("id") || !item.GetArray("content")) return CorruptRun();
        auto message = PublicMessage(item, run, created, finished);
        auto detail = Object(
            {{"type", String("message_creation")},
             {"message_creation", Object({{"message_id", *item.Find("id")}})}});
        view->messages.array.push_back(std::move(message));
        view->steps.array.push_back(Step(run, item, "message_creation",
                                         std::move(detail), state, created,
                                         finished));
      } else if (type == "function_call" || type == "file_search_call") {
        Json tool;
        auto step_end = finished;
        if (type == "function_call") {
          const auto call = item.GetString("call_id");
          const auto* submitted = entry.Find("submitted");
          const auto* result = submitted ? submitted->Find(call) : nullptr;
          auto fn =
              Object({{"name", String(item.GetString("name"))},
                      {"arguments", String(item.GetString("arguments"))}});
          tool = Object({{"type", String("function")},
                         {"id", String(call)},
                         {"function", fn}});
          if (const auto* settled = entry.Find("settled_calls");
              result && settled && settled->Has(call))
            step_end = settled->Find(call)->GetInt("at");
          if (!result && stage == "completed") calls.array.push_back(tool);
          Set(&Field(tool, "function"), "output", result ? *result : Json());
          if (!result && state == "completed") state = "in_progress";
        } else {
          tool = Object({{"type", String("file_search")},
                         {"id", *item.Find("id")},
                         {"file_search", SearchDetails(item)}});
        }
        auto detail = Object({{"type", String("tool_calls")},
                              {"tool_calls", Array({std::move(tool)})}});
        view->steps.array.push_back(Step(run, item, "tool_calls",
                                         std::move(detail), state, created,
                                         step_end));
      }
    }
    if (stage == "failed") {
      const auto* e = response->Find("error");
      Set(&run, "last_error",
          Object({{"code", String("server_error")},
                  {"message", String(e ? e->GetString("message", "Run failed")
                                       : "Run failed")}}));
    }
    if (stage == "incomplete") {
      Set(&run, "incomplete_details",
          Object({{"reason", String("max_completion_tokens")}}));
    }
  }
  const auto prompt_limit = run.GetInt("max_prompt_tokens");
  const auto output_limit = run.GetInt("max_completion_tokens");
  const bool pending = !calls.array.empty();
  auto status = run.GetString("status");
  const bool prompt_exhausted =
      prompt_limit > 0 &&
      (input > prompt_limit || (pending && input == prompt_limit));
  const bool output_exhausted =
      output_limit > 0 &&
      (output > output_limit || (pending && output == output_limit));
  if (status == "completed" && (prompt_exhausted || output_exhausted)) {
    status = "incomplete";
    Set(&run, "incomplete_details",
        Object(
            {{"reason", String(prompt_exhausted ? "max_prompt_tokens"
                                                : "max_completion_tokens")}}));
  } else if (status == "completed" && !calls.array.empty()) {
    if (entry.GetInt("cancelled_at")) {
      status = "cancelled";
      finished = entry.GetInt("cancelled_at");
    } else if (std::time(nullptr) >= run.GetInt("expires_at")) {
      status = "expired";
      finished = run.GetInt("expires_at");
    } else {
      status = "requires_action";
      Set(&run, "required_action",
          Object({{"type", String("submit_tool_outputs")},
                  {"submit_tool_outputs",
                   Object({{"tool_calls", std::move(calls)}})}}));
    }
  }
  Set(&run, "status", String(status));
  if (status == "failed" || status == "completed" || status == "cancelled")
    Set(&run, status + "_at", At(finished));
  if (!ActiveRun(run) && has_usage) Set(&run, "usage", Usage(input, output));
  if (status == "cancelled" || status == "expired" || status == "incomplete") {
    const auto terminal =
        status == "incomplete" ? std::string("cancelled") : status;
    for (auto& step : view->steps.array)
      if (step.GetString("status") == "in_progress") {
        Set(&step, "status", String(terminal));
        Set(&step, terminal + "_at", At(finished));
      }
  }
  ChatPlanError error;
  if (!ValidateAssistant(run, AssistantShape::kRunResource, &error))
    return CorruptRun();
  for (const auto& step : view->steps.array)
    if (!ValidateAssistant(step, AssistantShape::kStepResource, &error))
      return CorruptRun();
  for (const auto& msg : view->messages.array)
    if (!ValidateAssistant(msg, AssistantShape::kMessageResource, &error))
      return CorruptRun();
  return {};
}

ObjectReply ObserveLastRun(ObjectStore& objects, const StateAccess& access,
                           Json* thread_data, bool require_idle) {
  auto* runs = thread_data->GetArray("runs");
  if (!runs || runs->array.empty() ||
      runs->array.back().GetBool("materialized"))
    return {};
  RunView view;
  auto found = ProjectRun(objects, access, runs->array.back(), &view);
  if (!found.ok()) return found;
  if (require_idle && ActiveRun(view.run))
    return Error(409, "thread_run_active", "The thread has an active run");
  auto& rows = Field(*thread_data, "messages").array;
  for (auto& message : view.messages.array) rows.push_back(std::move(message));
  if (!ActiveRun(view.run))
    Set(&Field(*thread_data, "runs").array.back(), "materialized",
        Boolean(true));
  return {};
}
}  // namespace q4t::server::assistant_detail
