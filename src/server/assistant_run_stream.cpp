#include "assistant_runs_internal.h"

#include <algorithm>
#include <set>
#include <thread>

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"

namespace q4t::server::assistant_detail {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
std::string Event(std::string_view name, const Json& data) {
  return "event: " + std::string(name) + "\r\ndata: " + OutputJson(data) +
         "\r\n\r\n";
}
std::string StreamError(void*, int status, const std::string& message,
                        std::string_view code, std::string_view param) {
  return Event(
      "error",
      Object({{"message", String(message)},
              {"type",
               String(status < 500 ? "invalid_request_error" : "server_error")},
              {"code", String(std::string(code))},
              {"param", param.empty() ? Json() : String(std::string(param))}}));
}
const Json* Row(const Json& list, const std::string& id) {
  const auto it = std::ranges::find_if(
      list.array, [&](const Json& r) { return r.GetString("id") == id; });
  return it == list.array.end() ? nullptr : &*it;
}

class RunStream {
 public:
  RunStream(int fd, const StateAccess& access) : fd_(fd), access_(access) {}
  bool Send(std::string_view name, const Json& data) {
    return detail::WriteAll(fd_, Event(name, data), {access_.deadline});
  }

  bool Replay(const Json& event, const RunView& view, bool include) {
    const auto type = event.GetString("type");
    const auto* item = event.Find("item");
    const auto id = item ? item->GetString("id") : event.GetString("item_id");
    const auto* step = Row(view.steps, "step_" + id);
    const auto* message = Row(view.messages, id);
    if (type == "response.in_progress" && !began_) {
      auto run = view.run;
      Set(&run, "status", String("in_progress"));
      for (const auto* field :
           {"required_action", "completed_at", "failed_at", "cancelled_at",
            "incomplete_details", "last_error", "usage"})
        Set(&run, field, {});
      began_ = true;
      return Send("thread.run.in_progress", run);
    }
    if (type == "response.output_item.added" && step) {
      seen_.insert(step->GetString("id"));
      auto start = *step;
      Set(&start, "status", String("in_progress"));
      for (const auto* key :
           {"completed_at", "failed_at", "cancelled_at", "expired_at"})
        Set(&start, key, {});
      if (auto& details = Field(start, "step_details");
          details.GetString("type") == "tool_calls")
        Set(&details, "tool_calls", Array());
      if (!Send("thread.run.step.created", start) ||
          !Send("thread.run.step.in_progress", start))
        return false;
      if (message) {
        auto initial = *message;
        Set(&initial, "content", Array());
        Set(&initial, "completed_at", {});
        Set(&initial, "incomplete_at", {});
        Set(&initial, "status", String("in_progress"));
        Set(&initial, "incomplete_details", {});
        return Send("thread.message.created", initial) &&
               Send("thread.message.in_progress", initial);
      }
      if (item->GetString("type") == "code_interpreter_call")
        return ToolDelta(
            *step,
            Object({{"index", Number(0)},
                    {"id", *item->Find("id")},
                    {"type", String("code_interpreter")},
                    {"code_interpreter", Object({{"input", String("")}})}}));
      if (item->GetString("type") == "function_call") {
        const auto call =
            Object({{"index", Number(0)},
                    {"id", *item->Find("call_id")},
                    {"type", String("function")},
                    {"function", Object({{"name", *item->Find("name")},
                                         {"arguments", String("")}})}});
        return ToolDelta(*step, call);
      }
    }
    if (type == "response.output_text.delta" ||
        type == "response.refusal.delta") {
      if (!message) return true;
      const bool refusal = type == "response.refusal.delta";
      auto part = Object({{"index", Number(event.GetInt("content_index"))},
                          {"type", String(refusal ? "refusal" : "text")}});
      if (refusal)
        Set(&part, "refusal", *event.Find("delta"));
      else
        Set(&part, "text", Object({{"value", *event.Find("delta")}}));
      return Send(
          "thread.message.delta",
          Object({{"id", String(id)},
                  {"object", String("thread.message.delta")},
                  {"delta", Object({{"content", Array({std::move(part)})}})}}));
    }
    if (type == "response.output_text.annotation.added" && message) {
      const auto index = event.GetInt("annotation_index", -1);
      const auto part_index = event.GetInt("content_index", -1);
      const auto* parts = message->GetArray("content");
      if (!parts || part_index < 0 ||
          static_cast<size_t>(part_index) >= parts->array.size())
        return false;
      const auto* text = parts->array[part_index].Find("text");
      const auto* refs = text ? text->GetArray("annotations") : nullptr;
      const auto* source = event.Find("annotation");
      if (!source || source->GetString("type") != "container_file_citation")
        return true;
      if (!refs) return false;
      const auto found =
          std::ranges::find_if(refs->array, [&](const Json& ref) {
            return ref.GetString("type") == "file_path" &&
                   ref.GetInt("start_index") == source->GetInt("start_index");
          });
      if (index < 0 || found == refs->array.end()) return false;
      auto ref = *found;
      Set(&ref, "index", Number(found - refs->array.begin()));
      auto part = Object(
          {{"index", Number(part_index)},
           {"type", String("text")},
           {"text", Object({{"annotations", Array({std::move(ref)})}})}});
      return Send(
          "thread.message.delta",
          Object({{"id", String(id)},
                  {"object", String("thread.message.delta")},
                  {"delta", Object({{"content", Array({std::move(part)})}})}}));
    }
    if (type == "response.code_interpreter_call_code.delta" && step)
      return ToolDelta(*step,
                       Object({{"index", Number(0)},
                               {"type", String("code_interpreter")},
                               {"code_interpreter",
                                Object({{"input", *event.Find("delta")}})}}));
    if (type == "response.output_item.done" && step && item &&
        item->GetString("type") == "code_interpreter_call" &&
        item->GetArray("outputs")) {
      auto outputs = Array();
      for (const auto& log : item->GetArray("outputs")->array) {
        if (log.GetString("type") != "logs") continue;
        auto delta = log;
        Set(&delta, "index", Number(outputs.array.size()));
        outputs.array.push_back(std::move(delta));
      }
      if (!outputs.array.empty() &&
          !ToolDelta(*step,
                     Object({{"index", Number(0)},
                             {"type", String("code_interpreter")},
                             {"code_interpreter",
                              Object({{"outputs", std::move(outputs)}})}})))
        return false;
    }
    if (type == "response.function_call_arguments.delta" && step)
      return ToolDelta(
          *step, Object({{"index", Number(0)},
                         {"type", String("function")},
                         {"function",
                          Object({{"arguments", *event.Find("delta")}})}}));
    if (type == "response.output_item.done" && step) {
      if (message && message->GetString("status") != "in_progress" &&
          !Send("thread.message." + message->GetString("status"), *message))
        return false;
      if (step->GetString("status") != "in_progress")
        return EndStep(*step, include);
    }
    return true;
  }
  bool EndStep(Json step, bool include) {
    if (!seen_.contains(step.GetString("id")) ||
        !finished_.insert(step.GetString("id")).second)
      return true;
    if (!include) {
      auto& detail = Field(step, "step_details");
      if (detail.Has("tool_calls"))
        for (auto& call : Field(detail, "tool_calls").array) {
          auto* search = call.Find("file_search");
          if (search && search->Has("results"))
            for (auto& row : Field(Field(call, "file_search"), "results").array)
              std::erase_if(row.object,
                            [](const auto& f) { return f.first == "content"; });
        }
    }
    return Send("thread.run.step." + step.GetString("status"), step);
  }

 private:
  bool ToolDelta(const Json& step, Json call) {
    auto detail = Object({{"type", String("tool_calls")},
                          {"tool_calls", Array({std::move(call)})}});
    return Send(
        "thread.run.step.delta",
        Object({{"id", *step.Find("id")},
                {"object", String("thread.run.step.delta")},
                {"delta", Object({{"step_details", std::move(detail)}})}}));
  }
  int fd_;
  const StateAccess& access_;
  bool began_ = false;
  std::set<std::string> seen_, finished_;
};
}  // namespace

void StreamAssistantRun(int fd, ObjectStore& objects, const StateAccess& access,
                        const Json& initial, AssistantRunRouteKind kind,
                        const std::string& response_id, bool include) {
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!http) return;
  int64_t sequence = 0;
  http->SetResponsesStream(StreamError, nullptr, &sequence);
  ScopeExit reset([&] { http->SetResponsesStream(nullptr, nullptr, nullptr); });
  if (!detail::BeginStreamingResponse(fd, http->State(), {access.deadline},
                                      detail::StreamStatus::kOk))
    return;
  RunStream stream(fd, access);
  bool first = true;
  size_t next = 0;
  const auto thread = initial.GetString("thread_id");
  const auto id = initial.GetString("id");
  auto heartbeat = Clock::now() + std::chrono::seconds(5);
  while ((!access.cancelled || !access.cancelled()) &&
         Clock::now() < access.deadline) {
    ObjectRequest state;
    auto loaded = LoadThreadDraft(objects, access, thread, &state);
    if (!loaded.ok()) {
      detail::SendError(fd, loaded.http_status, loaded.ClientMessage(),
                        {access.deadline}, loaded.ClientCode());
      return;
    }
    const auto* runs = state.data.GetArray("runs");
    const Json* current = nullptr;
    if (runs) {
      for (const auto& row : runs->array) {
        if (const auto* run = row.Find("run");
            run && run->GetString("id") == id) {
          current = &row;
          break;
        }
      }
    }
    if (!current) {
      detail::SendError(fd, 404, "Run no longer exists", {access.deadline},
                        "run_not_found");
      return;
    }
    auto entry = *current;
    auto& ids = Field(entry, "response_ids").array;
    const auto found = std::ranges::find_if(
        ids, [&](const Json& v) { return v.str == response_id; });
    if (found == ids.end()) {
      detail::SendError(fd, 503, "Run generation changed", {access.deadline},
                        "assistant_run_invalid");
      return;
    }
    const bool advanced = found + 1 != ids.end();
    ids.erase(found + 1, ids.end());
    auto record = ResponsesState(objects).ReadRecord(response_id, access);
    if (!record.ok()) {
      detail::SendError(fd, record.http_status, record.ClientMessage(),
                        {access.deadline}, record.ClientCode());
      return;
    }
    if (advanced) {
      const auto& outputs =
          record.value.Find("response")->GetArray("output")->array;
      auto& submitted = Field(entry, "submitted").object;
      for (const auto& call : outputs)
        if (call.GetString("type") == "function_call")
          std::erase_if(submitted, [&](const auto& field) {
            return field.first == call.GetString("call_id");
          });
    }
    RunView view;
    auto projected = ProjectRun(objects, access, entry, &view);
    if (!projected.ok()) {
      detail::SendError(fd, projected.http_status, projected.ClientMessage(),
                        {access.deadline}, projected.ClientCode());
      return;
    }
    if (first) {
      if (kind == AssistantRunRouteKind::kCreateThread &&
          !stream.Send("thread.created", *state.data.Find("thread")))
        return;
      if (kind != AssistantRunRouteKind::kSubmit &&
          !stream.Send("thread.run.created", initial))
        return;
      if (!stream.Send("thread.run.queued", initial)) return;
      if (kind == AssistantRunRouteKind::kSubmit) {
        const auto* settlements = entry.Find("settled_calls");
        for (const auto& step : view.steps.array) {
          const auto* calls = step.Find("step_details")->GetArray("tool_calls");
          if (!calls || calls->array.empty() || !settlements) continue;
          const auto* origin =
              settlements->Find(calls->array[0].GetString("id"));
          if (origin && origin->GetString("response_id") == response_id &&
              !stream.Send("thread.run.step.completed", step))
            return;
        }
      }
      first = false;
    }
    const auto& events = record.value.GetArray("events")->array;
    while (next < events.size())
      if (!stream.Replay(events[next++], view, include)) return;
    const std::string status = view.run.GetString("status");
    const auto phase = record.value.Find("response")->GetString("status");
    if (phase != "queued" && phase != "in_progress" && status != "queued" &&
        status != "in_progress" && status != "cancelling") {
      for (const auto& step : view.steps.array)
        if (step.GetString("status") != "in_progress" &&
            !stream.EndStep(step, include))
          return;
      if (!stream.Send("thread.run." + status, view.run)) return;
      http->BeforeTerminal();
      if (detail::WriteAll(fd, "event: done\r\ndata: [DONE]\r\n\r\n",
                           {access.deadline}))
        *http->State() = detail::ResponseState::kComplete;
      return;
    }
    if (Clock::now() >= heartbeat) {
      if (!detail::WriteAll(fd, ": keepalive\r\n\r\n", {access.deadline}))
        return;
      heartbeat = Clock::now() + std::chrono::seconds(5);
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(75));
  }
  if (!access.cancelled || !access.cancelled())
    detail::SendError(fd, 408, "Run stream deadline expired", {access.deadline},
                      "request_timeout");
}
}  // namespace q4t::server::assistant_detail
