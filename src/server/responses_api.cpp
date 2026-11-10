#include "q4t/server/responses_api.h"

#include <algorithm>
#include <thread>

#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/responses_jobs.h"
#include <ctime>

namespace q4t::server {
void ServeBackgroundResponse(int fd, ObjectStore& objects, ResponsesJobs& jobs,
                             const io::Json& request,
                             const ChatPlanConfig& config,
                             const StateAccess& access) {
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Allowed()) {
    detail::SendError(fd, 503, "Background identity unavailable", {},
                      "object_scope_unavailable");
    return;
  }
  auto run = std::make_unique<ResponsesExecution>(objects);
  auto result = run->Prepare(request, config, access, std::time(nullptr), true);
  if (!result.ok()) {
    detail::SendError(fd, result.http_status, result.ClientMessage(), {},
                      result.ClientCode(),
                      result.value.GetString("param"));
    return;
  }
  ServePreparedBackgroundResponse(fd, objects, jobs, std::move(run), access);
}

void ServePreparedBackgroundResponse(
    int fd, ObjectStore& objects, ResponsesJobs& jobs,
    std::unique_ptr<ResponsesExecution> run, const StateAccess& access) {
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Allowed() || !run || !run->Prepared() ||
      run->Begun() || !run->Envelope().background ||
      run->Scope() != access.scope) {
    detail::SendError(fd, 503, "Prepared background owner unavailable", {},
                      "object_scope_unavailable");
    return;
  }
  const auto id = run->Id();
  if (!run->BindRateDomain(http->RateDomain())) {
    detail::SendError(fd, 503, "Background accounting origin changed", {},
                      "response_rate_domain_changed");
    return;
  }
  const bool stream = run->Envelope().stream;
  // Construct the subscriber target before transferring run: a fast owned
  // job can finish and release it immediately after Submit returns.
  std::string target;
  if (stream) {
    target = "/v1/responses/" + id + "?stream=true";
    const auto& request = run->Envelope().request;
    if (const auto* options = request.Find("stream_options");
        options && !options->GetBool("include_obfuscation", true))
      target += "&include_obfuscation=false";
    if (const auto* include = request.GetArray("include"))
      for (const auto& item : include->array)
        target += "&include[]=" + item.str;
  }
  auto result =
      jobs.Submit(std::move(run), http->Client(), [&](ClientLease& lease) {
        return http->TransferClientLease(lease);
      });
  if (!result.ok()) {
    detail::SendError(fd, result.http_status, result.ClientMessage(), {},
                      result.ClientCode(),
                      result.value.GetString("param"));
    return;
  }
  if (stream) {
    ServeResponsesState(fd, objects, "GET", target, "", access, &jobs);
  } else {
    detail::SendSimple(fd, 200, "OK", OutputJson(result.value),
                       "application/json", {access.deadline});
  }
}

namespace {
using namespace object_detail;
ObjectReply Invalid(std::string message, std::string param) {
  auto result = Error(400, "invalid_request", std::move(message));
  result.value = Object({{"param", String(std::move(param))}});
  return result;
}
ObjectReply Method(std::string allowed) {
  auto result = Error(405, "method_not_allowed", "Method not allowed");
  result.value = Object(
      {{"param", String("method")}, {"allow", String(std::move(allowed))}});
  return result;
}
void Remove(Json* value, std::string_view key) {
  std::erase_if(value->object,
                [&](const auto& field) { return field.first == key; });
}
bool Terminal(std::string_view status) {
  return status == "completed" || status == "incomplete" ||
         status == "failed" || status == "cancelled";
}
Json ViewResponse(Json response, const ItemViewOptions& view) {
  for (auto& item : Field(response, "output").array)
    item = ProjectStoredItem(item, view);
  return response;
}
Json ViewEvent(Json event, const ResponsesReadPlan& plan) {
  if (event.Has("response"))
    Set(&event, "response", ViewResponse(Field(event, "response"), plan.view));
  if (event.Has("item"))
    Set(&event, "item", ProjectStoredItem(Field(event, "item"), plan.view));
  if (std::find(plan.view.include.begin(), plan.view.include.end(),
                "message.output_text.logprobs") == plan.view.include.end()) {
    // The text delta/done event shape requires this array even when the
    // caller did not request probability contents. Other event types must
    // not acquire an unrelated field.
    if (event.Has("logprobs")) Set(&event, "logprobs", Array());
    if (event.Has("part")) Remove(&Field(event, "part"), "logprobs");
  }
  Remove(&event, "obfuscation");
  if (plan.obfuscation && event.GetString("type").ends_with(".delta")) {
    const auto random = detail::NewRequestId("");
    const size_t size = 128 - ((OutputJson(event).size() + 18) % 128) +
                        ((random[0] & 1) ? 128 : 0);
    std::string padding;
    while (padding.size() < size) padding += detail::NewRequestId("");
    padding.resize(size);
    Set(&event, "obfuscation", String(std::move(padding)));
  }
  return event;
}
struct StreamState {
  int64_t sequence = 0;
  static std::string ErrorEvent(void* owner, int, const std::string& message,
                                std::string_view code, std::string_view param) {
    auto& state = *static_cast<StreamState*>(owner);
    // This subscriber failed; the underlying response might still complete.
    const auto event = Object(
        {{"type", String("error")},
         {"sequence_number", Number(state.sequence)},
         {"code", String(std::string(code))},
         {"message", String(message)},
         {"param", param.empty() ? Json() : String(std::string(param))}});
    return "event: error\r\ndata: " + OutputJson(event) + "\r\n\r\n";
  }
};
}  // namespace

ObjectReply PlanResponsesRead(std::string_view method, std::string_view target,
                              std::string_view body, ResponsesReadPlan* plan) {
  *plan = {};
  constexpr std::string_view root = "/v1/responses";
  const auto separator = target.find('?');
  auto path = target.substr(0, separator);
  if (!IsResponsesTarget(path) || target.find('#') != target.npos)
    return Error(404, "route_not_found", "Response route not found");
  if (path == root || IsResponsesCompaction(path) || IsResponsesTokenCount(path))
    return Method("POST");
  path.remove_prefix(root.size() + 1);
  const auto end = path.find('/');
  plan->id = path.substr(0, end);
  if (!ValidId(plan->id))
    return Invalid("Invalid response identity", "response_id");
  const bool items = end != path.npos && path.substr(end) == "/input_items";
  const bool cancel = end != path.npos && path.substr(end) == "/cancel";
  if (end != path.npos && !items && !cancel)
    return Error(404, "route_not_found", "Response route not found");
  if (items && method != "GET") return Method("GET");
  if (cancel && method != "POST") return Method("POST");
  if (!items && !cancel && method != "GET" && method != "DELETE")
    return Method("GET, DELETE");
  if (!body.empty()) return Invalid("Request body is not allowed", "body");
  auto query = ParseApplicationQuery(
      separator == target.npos ? "" : target.substr(separator + 1));
  if (!query.ok()) return query;
  if (cancel) {
    if (!query.value.object.empty())
      return Invalid("Query is not allowed on cancellation", "query");
    plan->operation = ResponsesReadPlan::Operation::kCancel;
    return {};
  }
  if (method == "DELETE") {
    if (!query.value.object.empty())
      return Invalid("Query is not allowed on deletion", "query");
    plan->operation = ResponsesReadPlan::Operation::kDelete;
    return {};
  }
  ChatPlanError error;
  if (items) {
    plan->operation = ResponsesReadPlan::Operation::kInputItems;
    auto status = ParseItemViewOptions(query.value, &plan->view, &error);
    return status ? ObjectReply() : Invalid(status.message(), error.param);
  }
  auto status = InputFields(
      query.value,
      {"include", "include_obfuscation", "starting_after", "stream"},
      &error.param, "query");
  if (!status) return Invalid(status.message(), error.param);
  plan->stream = query.value.GetBool("stream", false);
  plan->obfuscation = query.value.GetBool("include_obfuscation", true);
  if (query.value.Has("starting_after")) {
    const auto after = query.value.GetInt("starting_after", -1);
    if (!plan->stream || after < 0)
      return Invalid("starting_after requires a nonnegative streaming cursor",
                     "starting_after");
    plan->starting_after = after;
  }
  auto includes = Object();
  if (query.value.Has("include"))
    Set(&includes, "include", Field(query.value, "include"));
  status = ParseItemViewOptions(includes, &plan->view, &error, false);
  return status ? ObjectReply() : Invalid(status.message(), error.param);
}

ObjectReply ReadResponsesState(ObjectStore& objects,
                               const ResponsesReadPlan& plan,
                               const StateAccess& access,
                               ResponsesReplay* replay) {
  *replay = {};
  replay->last_sequence = plan.starting_after;
  if (plan.operation == ResponsesReadPlan::Operation::kCancel)
    return Error(500, "response_executor_required",
                 "Cancellation requires the execution owner");
  ResponsesState state(objects);
  if (plan.operation == ResponsesReadPlan::Operation::kDelete) {
    auto result = state.Erase(plan.id, access);
    if (result.ok())
      result.value = Object({{"id", String(plan.id)},
                             {"object", String("response.deleted")},
                             {"deleted", Boolean(true)}});
    return result;
  }
  auto stored = state.ReadRecord(plan.id, access);
  if (!stored.ok()) return stored;
  if (plan.operation == ResponsesReadPlan::Operation::kInputItems)
    return PageStoredItems(Field(stored.value, "history"), plan.view);
  auto response = ViewResponse(Field(stored.value, "response"), plan.view);
  if (plan.stream) {
    if (response.GetBool("background") &&
        !Field(stored.value, "request").GetBool("stream"))
      return Invalid("Background response was not created with stream=true",
                     "stream");
    const auto& events = Field(stored.value, "events").array;
    replay->terminal = Terminal(response.GetString("status"));
    if (replay->terminal && events.empty())
      return Error(503, "response_events_unavailable",
                   "Original response events are not available");
    if (replay->terminal && response.GetString("status") != "cancelled" &&
        (events.back().GetString("type") !=
             "response." + response.GetString("status") ||
         !events.back().Find("response") ||
         events.back().Find("response")->GetString("id") != plan.id))
      return Error(503, "response_state_invalid",
                   "Retained events do not match response terminal");
    if (!events.empty() &&
        plan.starting_after >= static_cast<int64_t>(events.size()))
      return Invalid("Cursor exceeds retained event sequence",
                     "starting_after");
    for (size_t i = 0; i < events.size(); ++i) {
      const auto& event = events[i];
      const auto type = event.GetString("type");
      const auto* sequence = event.Find("sequence_number");
      if ((!type.starts_with("response.") && type != "error") ||
          type.find_first_not_of("abcdefghijklmnopqrstuvwxyz._") != type.npos ||
          !sequence || !IsInputInteger(*sequence) ||
          sequence->AsInt() != static_cast<int64_t>(i))
        return Error(503, "response_state_invalid", "Invalid retained event");
      if (static_cast<int64_t>(i) <= plan.starting_after) continue;
      const auto wire = "event: " + type +
                        "\r\ndata: " + OutputJson(ViewEvent(event, plan)) +
                        "\r\n\r\n";
      constexpr size_t limit = 32 * 1024 * 1024;
      if (wire.size() > limit || replay->wire.size() > limit - wire.size())
        return Error(503, "response_replay_capacity",
                     "Response event view exceeds replay capacity");
      replay->wire += wire;
      replay->last_sequence = i;
    }
  }
  ObjectReply result;
  result.value = std::move(response);
  return result;
}

void ServeResponsesState(int fd, ObjectStore& objects, std::string_view method,
                         std::string_view target, std::string_view body,
                         const StateAccess& access, ResponsesJobs* jobs) {
  auto* http = detail::HttpResponseContext::Current(fd);
  StreamState stream;
  ScopeExit clear_hooks([&] {
    if (http) http->SetResponsesStream(nullptr, nullptr, nullptr);
  });
  const auto send_error = [&](const ObjectReply& reply) {
    detail::SendError(fd, reply.http_status, reply.ClientMessage(), {},
                      reply.ClientCode(),
                      reply.value.GetString("param"),
                      reply.value.GetString("allow"));
  };
  try {
    ResponsesReadPlan plan;
    auto result = PlanResponsesRead(method, target, body, &plan);
    if (!result.ok()) {
      send_error(result);
      return;
    }
    if (plan.operation == ResponsesReadPlan::Operation::kCancel) {
      result = jobs ? jobs->Cancel(plan.id, access)
                    : Error(503, "background_unavailable",
                            "Background executor unavailable");
      if (result.ok())
        detail::SendSimple(fd, 200, "OK", OutputJson(result.value),
                           "application/json", {access.deadline});
      else
        send_error(result);
      return;
    }
    if (plan.stream && http)
      http->SetResponsesStream(&StreamState::ErrorEvent, &stream,
                               &stream.sequence);
    bool started = false;
    auto heartbeat = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    for (;;) {
      if (access.cancelled && access.cancelled()) return;
      if (std::chrono::steady_clock::now() >= access.deadline) {
        send_error(Error(408, "response_stream_timeout",
                         "Response query deadline exceeded"));
        return;
      }
      ResponsesReplay replay;
      result = ReadResponsesState(objects, plan, access, &replay);
      if (!result.ok()) {
        send_error(result);
        return;
      }
      if (!plan.stream) {
        detail::SendSimple(fd, 200, "OK", OutputJson(result.value),
                           "application/json", {access.deadline});
        return;
      }
      if (!http) return;
      if (!started &&
          !detail::BeginStreamingResponse(fd, http->State(), {access.deadline}))
        return;
      started = true;
      if (!detail::WriteAll(fd, replay.wire, {access.deadline})) return;
      // Subscriber errors must not consume a durable resume position.
      stream.sequence = std::max<int64_t>(0, replay.last_sequence);
      plan.starting_after = replay.last_sequence;
      if (replay.terminal) {
        http->BeforeTerminal();
        // The entire retained terminal (or empty resume after it) has already
        // been written. Mark completion so the connection owner emits the
        // final HTTP chunk; kTerminal requires a subsequent successful write.
        *http->State() = detail::ResponseState::kComplete;
        return;
      }
      if (std::chrono::steady_clock::now() >= heartbeat) {
        if (!detail::WriteAll(fd, ": keepalive\r\n\r\n", {access.deadline}))
          return;
        heartbeat = std::chrono::steady_clock::now() + std::chrono::seconds(5);
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
  } catch (const std::bad_alloc&) {
    detail::SendExceptionResponse(
        fd, http ? *http->State() : detail::ResponseState::kUnsent,
        detail::ExceptionKind::kHostMemory);
  } catch (...) {
    detail::SendExceptionResponse(
        fd, http ? *http->State() : detail::ResponseState::kUnsent);
  }
}
}  // namespace q4t::server
