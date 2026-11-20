#include "q4t/server/live_client_context.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace std::chrono_literals;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
ObjectReply Invalid(std::string_view message, std::string param = {}) {
  auto reply = Error(400, "live_context_invalid", std::string(message));
  if (!param.empty())
    reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}
ObjectReply Unavailable() {
  return Error(503, "live_context_unavailable", "Live context is not active");
}
Json Tool() {
  return Object(
      {{"type", String("function")},
       {"name", String(std::string(kLiveDelegateTool))},
       {"description", String("Hand a task to the application. This creates a "
                              "handoff; it does not complete the task.")},
       {"parameters",
        Object({{"type", String("object")},
                {"properties",
                 Object({{"task", Object({{"type", String("string")},
                                          {"minLength", Number(1)},
                                          {"maxLength", Number(4096)}})}})},
                {"required", Array({String("task")})},
                {"additionalProperties", Boolean(false)}})}});
}
bool OwnedTool(const Json& settings) {
  const auto* tools = settings.GetArray("tools");
  return tools && tools->array.size() == 1 &&
         OutputJson(tools->array[0]) == OutputJson(Tool());
}
Json ContextItem(std::string text) {
  return Object(
      {{"type", String("message")},
       {"role", String("user")},
       {"content", Array({Object({{"type", String("input_text")},
                                  {"text", String(std::move(text))}})})}});
}
Json NoDelegation() { return Object({{"tool_choice", String("none")}}); }
}  // namespace
Json LiveClientFrontendSettings(Json settings) {
  Set(&settings, "tools", Array({Tool()}));
  Set(&settings, "tool_choice", String("auto"));
  auto instructions = settings.GetString("instructions");
  instructions +=
      "\n\nLive voice adapter: Speak naturally and briefly. For tasks "
      "requiring "
      "the application, call q4t_live_delegate. Its acknowledgement only "
      "confirms handoff creation; never present it as a task result. Wait for "
      "application context before reporting results. Do not delegate an "
      "application acknowledgement or supplied commentary back to itself.";
  Set(&settings, "instructions", String(std::move(instructions)));
  return settings;
}
LiveClientContext::LiveClientContext(std::string owner, LiveFrontend& frontend,
                                     Json settings,
                                     LiveClientContextCallbacks callbacks)
    : owner_(std::move(owner)),
      frontend_(frontend),
      settings_(std::move(settings)),
      callbacks_(std::move(callbacks)) {
  size_t bytes = 0;
  valid_ = ValidRequestId(owner_) && callbacks_.count_text && callbacks_.emit &&
           OwnedTool(settings_) &&
           settings_.GetString("instructions").size() <= kInstructionsBytes &&
           MeasureRealtimeStorage(settings_, 4ULL << 20, &bytes).ok();
}
LiveClientContext::Clock::time_point LiveClientContext::Now() const {
  return callbacks_.now ? callbacks_.now() : Clock::now();
}
bool LiveClientContext::Known(std::string_view id) const {
  return id.size() <= 256 && entries_.contains(std::string(id));
}
ObjectReply LiveClientContext::AcknowledgementDelay(uint32_t milliseconds) {
  if (!valid_ || closed_ || failed_) return Unavailable();
  if (milliseconds > INT32_MAX)
    return Invalid("Acknowledgement delay exceeds native duration");
  delay_ms_ = milliseconds;
  return {};
}
ObjectReply LiveClientContext::Fail(ObjectReply reply) {
  failed_ = true;
  return reply;
}
ObjectReply LiveClientContext::Emit(Json event) {
  auto reply = callbacks_.emit(std::move(event));
  if (!Accepted(reply)) {
    // The actual context/handoff transition preceded this publication. It
    // cannot be retried just because the transport did not confirm reception.
    reply.commit_unknown = true;
    return Fail(std::move(reply));
  }
  return {};
}
void LiveClientContext::CancelAcknowledgements() {
  for (auto& [id, entry] : entries_) {
    (void)id;
    entry.pending = false;
    if (!entry.response_id.empty()) frontend_.CancelResponse(entry.response_id);
  }
}
ObjectReply LiveClientContext::Append(const Json& event) {
  if (!valid_ || closed_ || failed_) return Unavailable();
  ChatPlanError error;
  const auto type = event.GetString("type");
  if ((type != "session.instructions.append" &&
       type != "session.thinking.append" &&
       type != "session.commentary.append") ||
      !ValidateLiveShape(event, LiveShape::kClientEvent, &error))
    return Invalid("Invalid Live context event", error.param);
  const auto* delegation = event.Find("delegation_id");
  if (!delegation || (!delegation->IsNull() &&
                      (!delegation->IsString() || !Known(delegation->str))))
    return Invalid("Unknown client delegation", "delegation_id");
  const auto text = event.GetString("content");
  if (text.size() > kInstructionsBytes)
    return Invalid("Context text exceeds its byte limit", "content");
  try {
    size_t count = 0;
    auto status = callbacks_.count_text(text, &count, &error);
    if (!status) return Error(error.http_status, error.code, status.message());
    if (count > 500)
      return Invalid("Context append exceeds 500 tokenizer tokens", "content");
    const auto client_event = event.GetString("event_id");
    ObjectReply reply;
    if (type == "session.instructions.append") {
      auto instructions = settings_.GetString("instructions");
      if (instructions.size() > kInstructionsBytes - 2 ||
          text.size() > kInstructionsBytes - 2 - instructions.size())
        return Invalid("Persistent instructions exceed their byte budget",
                       "content");
      instructions += "\n\n" + text;
      auto next = settings_;
      Set(&next, "instructions", String(std::move(instructions)));
      reply =
          frontend_.Settings(next, !text.empty(), client_event, NoDelegation());
      if (Accepted(reply)) settings_ = std::move(next);
    } else {
      const bool spoken = type == "session.commentary.append";
      auto content =
          std::string(
              spoken ? "Application commentary to communicate to the caller: "
                     : "Application background context, not a new user "
                       "request: ") +
          text;
      reply = frontend_.Create(ContextItem(std::move(content)), spoken,
                               client_event, NoDelegation());
      if (Accepted(reply)) CancelAcknowledgements();
    }
    if (!Accepted(reply)) return reply;
    const auto position = frontend_.Timeline() / 48;
    auto acknowledgement = Object({{"type", String(type + "ed")},
                                   {"start_ms", Number(position)},
                                   {"end_ms", Number(position)}});
    if (!client_event.empty())
      Set(&acknowledgement, "client_event_id", String(client_event));
    return Emit(std::move(acknowledgement));
  } catch (...) {
    return Fail(Error(503, "live_context_failed", "Context callback failed"));
  }
}
ObjectReply LiveClientContext::Verify(const RealtimeConversationJob& job,
                                      const RealtimeGenerationResult& result,
                                      const Json& output) const {
  if (job.snapshot.owner != owner_ || !job.native_input ||
      !job.in_conversation || !OwnedTool(job.settings) || !output.IsArray())
    return Invalid("Native delegation lost its frontend owner");
  const auto* calls = result.message.GetArray("tool_calls");
  const auto count = calls ? calls->array.size() : 0;
  if (count > kMaximumDelegations - entries_.size())
    return Error(429, "live_delegation_capacity",
                 "Client delegation ledger is full");
  if (count && job.response.GetString("tool_choice") == "none")
    return Invalid("Delegation is disabled for this frontend response");
  size_t position = 0;
  std::set<std::string> current;
  for (const auto& item : output.array) {
    if (item.GetString("type") == "message") continue;
    if (item.GetString("type") != "function_call" || !calls ||
        position == count || item.GetString("status") != "completed")
      return Invalid("Expected a complete native delegation call");
    const auto& call = calls->array[position++];
    const auto* function = call.Find("function");
    const auto id = call.GetString("id");
    if (!function || call.GetString("type") != "function" ||
        function->GetString("name") != kLiveDelegateTool ||
        item.GetString("name") != kLiveDelegateTool ||
        item.GetString("call_id") != id || !ValidRequestId(id) ||
        item.GetString("arguments") != function->GetString("arguments") ||
        calls_.contains(id) || !current.insert(id).second)
      return Invalid("Native delegation identity or finite result differs");
    Json arguments;
    if (!io::ParseJson(function->GetString("arguments"), &arguments, true,
                       {8, 16, 32768, true}) ||
        !arguments.IsObject() || arguments.object.size() != 1 ||
        !arguments.Find("task") || !arguments.Find("task")->IsString())
      return Invalid("Invalid native delegation arguments");
    const auto& task = arguments.Find("task")->str;
    const auto characters = std::count_if(task.begin(), task.end(), [](char c) {
      return (static_cast<unsigned char>(c) & 0xc0) != 0x80;
    });
    if (task.empty() || task.size() > 16384 || characters > 4096 ||
        std::all_of(task.begin(), task.end(), [](char c) {
          return c == ' ' || c == '\t' || c == '\r' || c == '\n';
        }))
      return Invalid("Empty or oversized native delegation task");
  }
  return position == count ? ObjectReply{}
                           : Invalid("Missing actual native delegation output");
}
ObjectReply LiveClientContext::Complete(const RealtimeConversationJob& job,
                                        const RealtimeGenerationResult& result,
                                        const Json& output,
                                        std::string_view client_event) {
  if (!valid_ || failed_) return Unavailable();
  if (closed_ || !Accepted(result.reply) ||
      result.state != RealtimeResponseEnd::State::kCompleted)
    return {};
  try {
    auto reply = Verify(job, result, output);
    if (!Accepted(reply)) return reply;
    for (const auto& item : output.array) {
      if (item.GetString("type") != "function_call") continue;
      const auto id = detail::NewRequestId("delegation_");
      const auto call_id = item.GetString("call_id");
      const auto due = Now() + std::chrono::milliseconds(delay_ms_);
      entries_.emplace(id, Entry{call_id, {}, due, due, due + 15s});
      calls_.insert(call_id);
      const auto receipt = Object({{"delegation_id", String(id)},
                                   {"target", String("client")},
                                   {"status", String("created")},
                                   {"task_completed", Boolean(false)}});
      reply =
          frontend_.Create(Object({{"type", String("function_call_output")},
                                   {"call_id", String(call_id)},
                                   {"output", String(OutputJson(receipt))}}));
      if (!Accepted(reply)) return Fail(std::move(reply));
      auto event =
          Object({{"type", String("session.delegation.created")},
                  {"delegation", Object({{"type", String("delegation")},
                                         {"id", String(id)},
                                         {"target", String("client")}})},
                  {"offset_ms", Number(frontend_.Timeline() / 48)}});
      if (!client_event.empty())
        Set(&event, "client_event_id", String(std::string(client_event)));
      reply = Emit(std::move(event));
      if (!Accepted(reply)) return reply;
    }
    return {};
  } catch (...) {
    return Fail(
        Error(503, "live_delegation_failed", "Delegation handoff failed"));
  }
}
ObjectReply LiveClientContext::Tick() {
  if (!valid_ || failed_) return Unavailable();
  if (closed_) return {};
  try {
    const auto now = Now();
    for (auto& [id, entry] : entries_) {
      (void)id;
      if (!entry.pending || now < entry.due || now < entry.retry) continue;
      if (now >= entry.deadline) {
        entry.pending = false;
        auto reply = Emit(Object(
            {{"type", String("error")},
             {"error",
              Object(
                  {{"type", String("server_error")},
                   {"code", String("live_acknowledgement_expired")},
                   {"message",
                    String(
                        "Spoken acknowledgement could not be scheduled")}})}}));
        if (!Accepted(reply)) return reply;
        continue;
      }
      auto reply = frontend_.Create(
          ContextItem("A task was handed to the application. Briefly "
                      "acknowledge that it is "
                      "being handled. No task result is available yet."),
          true, {}, NoDelegation());
      if (!Accepted(reply)) {
        if (reply.http_status == 429 && !reply.commit_unknown) {
          entry.retry = now + 100ms;
          continue;
        }
        return Fail(std::move(reply));
      }
      entry.response_id = reply.value.GetString("response_id");
      if (!ValidRequestId(entry.response_id)) return Fail(Unavailable());
      entry.pending = false;
    }
    return {};
  } catch (...) {
    return Fail(Error(503, "live_acknowledgement_failed",
                      "Acknowledgement callback failed"));
  }
}
void LiveClientContext::Close() noexcept {
  closed_ = true;
  for (auto& [id, entry] : entries_) {
    (void)id;
    entry.pending = false;
    if (!entry.response_id.empty()) frontend_.CancelResponse(entry.response_id);
  }
}
}  // namespace q4t::server
