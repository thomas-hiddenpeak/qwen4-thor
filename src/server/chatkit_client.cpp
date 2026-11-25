#include "q4t/server/chatkit.h"

#include <algorithm>
#include <ctime>
#include <thread>

#include "chatkit_credentials.h"
#include "chatkit_retry.h"
#include "chatkit_structured.h"
#include "chatkit_attachments.h"
#include "chatkit_thread_state.h"
#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_jobs.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using chatkit_detail::Grant;
using Clock = std::chrono::steady_clock;

ObjectReply Invalid(std::string message) {
  return Error(400, "invalid_request", std::move(message));
}

ObjectReply UserInput(ObjectStore& objects, const StateAccess& access,
                      const Grant& grant, const Json& input,
                      const std::string& id, const std::string& thread_id,
                      int64_t now, Json* user, Json* model_message) {
  const auto* parts = input.GetArray("content");
  const auto* attachments = input.GetArray("attachments");
  const auto* options = input.Find("inference_options");
  if (!parts || !attachments ||
      (parts->array.empty() && attachments->array.empty()) || !options ||
      !options->IsObject())
    return Invalid(
        "Expected message content, attachments and inference options");
  Json content = Array(), model = Array(), files = Array();
  auto attached = chatkit_detail::ResolveAttachments(
      objects, access, grant, *attachments, &files, &model);
  if (!attached.ok()) return attached;
  if (const auto* quote = input.Find("quoted_text");
      quote && !quote->IsNull()) {
    if (!quote->IsString()) return Invalid("Expected quoted text");
    content.array.push_back(
        Object({{"type", String("quoted_text")}, {"text", *quote}}));
    model.array.push_back(
        Object({{"type", String("input_text")}, {"text", *quote}}));
  }
  auto projected = chatkit_detail::AppendUserContent(*parts, &content, &model);
  if (!projected.ok()) return projected;
  *user = Object(
      {{"id", String(id)},
       {"thread_id", String(thread_id)},
       {"type", String("chatkit.user_message")},
       {"object", String("chatkit.thread_item")},
       {"created_at", Number(now)},
       {"content", std::move(content)},
       {"attachments", std::move(files)},
       {"inference_options",
        options->object.empty()
            ? Json{}
            : Object({{"model", options->Has("model") ? *options->Find("model")
                                                      : Json{}},
                      {"tool_choice", options->Has("tool_choice")
                                          ? *options->Find("tool_choice")
                                          : Json{}}})}});
  *model_message =
      Object({{"role", String("user")}, {"content", std::move(model)}});
  return {};
}

ObjectReply Submit(int fd, ObjectStore& objects, ResponsesJobs& jobs,
                   const ChatPlanConfig& config, const StateAccess& access,
                   const Grant& grant, const Json& params,
                   std::string_view operation, std::string* thread_id,
                   std::string* response_id, std::string* form_tool,
                   Json* user) {
  const auto& session = *grant.data.Find("session");
  const auto& workflow = *session.Find("workflow");
  const auto* features = session.Find("chatkit_configuration");
  const bool create = operation == "threads.create";
  const bool tool_output = operation == "threads.add_client_tool_output";
  const bool structured = operation == "threads.add_structured_input";
  const bool retry = operation == "threads.retry_after_item";
  const auto* input = params.Find("input");
  if (retry || structured) {
    for (const auto* field : {"thread_id", "item_id"}) {
      const auto* value = params.Find(field);
      if (!value || !value->IsString()) {
        auto error = Invalid("Expected a thread item identifier");
        error.value = Object({{"param", String(field)}});
        return error;
      }
    }
  }
  if (!retry && !structured && !tool_output && (!input || !input->IsObject()))
    return Invalid("Missing user message");
  ObjectRequest write;
  write.kind = ObjectKind::kChatKitThread;
  write.operation = ObjectOperation::kPut;
  write.scope = access.scope;
  write.id = create ? NewObjectId(write.kind) : params.GetString("thread_id");
  if (!write.id.starts_with("cthr_") || !ValidApplicationId(write.id))
    return Invalid("Expected a thread identifier");
  const auto now = std::time(nullptr);
  if (create) {
    auto thread = Object({{"id", String(write.id)},
                          {"object", String("chatkit.thread")},
                          {"created_at", Number(now)},
                          {"title", {}},
                          {"status", Object({{"type", String("active")}})},
                          {"user", String(session.GetString("user"))}});
    write.data = Object({{"thread", std::move(thread)},
                         {"items", Array()},
                         {"workflow", String(workflow.GetString("id"))},
                         {"created_by", String(grant.session)}});
    const bool history = features->Find("history")->GetBool("enabled");
    Set(&write.data, "session_only", Boolean(!history));
    write.ttl_seconds =
        history ? 0 : std::max<int64_t>(1, session.GetInt("expires_at") - now);
  } else {
    auto old = chatkit_detail::ReadThread(objects, access, write.id, &grant);
    if (!old.ok()) return old;
    write.expected_revision = old.value.GetInt("revision");
    write.expires_at = old.value.GetInt("expires_at");
    write.data = Field(old.value, "data");
    if (!write.data.GetString("pending_response").empty())
      return Error(409, "chatkit_thread_busy",
                   "A thread response is in progress");
  }
  auto& items = Field(write.data, "items").array;
  const bool waiting =
      !items.empty() &&
      items.back().GetString("type") == "chatkit.client_tool_call" &&
      items.back().GetString("status") == "in_progress";
  Json model_message;
  chatkit_detail::RetryHistory prefix;
  if (retry) {
    ResponsesState state(objects);
    auto record = state.ReadRecord(write.data.GetString("last_response"), access);
    if (!record.ok()) return record;
    auto selected = chatkit_detail::SelectRetryHistory(
        record.value, params.GetString("item_id"), &write.data, user, &prefix);
    if (!selected.ok() || prefix.items.IsNull()) return selected;
    // Resolve the original scoped item. Its stored status and file snapshot
    // belong to the server and cannot be sent as new client input fields.
    model_message = Object({{"type", String("item_reference")},
                            {"id", *prefix.items.array.back().Find("id")}});
  } else if (tool_output || structured) {
    if (structured) {
      const auto item_id = params.GetString("item_id");
      const auto it = std::ranges::find_if(items, [&](const Json& row) {
        return row.GetString("id") == item_id;
      });
      if (it == items.end())
        return Error(404, "chatkit_item_not_found", "Thread item not found");
      if (!it->Has("browser_form")) {
        auto error = Invalid("Expected a structured input item");
        error.value = Object({{"param", String("item_id")}});
        return error;
      }
      if (!waiting || &*it != &items.back() ||
          it->GetString("status") != "in_progress")
        return Error(409, "chatkit_input_not_pending", "Input is not pending");
    }
    if (!waiting)
      return Error(409, "chatkit_no_pending_tool", "No pending client tool");
    auto& last = items.back();
    const auto* result = params.Find("result");
    if (structured) {
      if (!input) return Invalid("Missing structured input");
      auto& form = Field(last, "browser_form");
      auto answered = chatkit_detail::SubmitStructuredInput(*input, &form);
      if (!answered.ok()) return answered;
      result = &form;
    } else if (last.Has("browser_form")) {
      return Error(409, "chatkit_structured_input_pending",
                   "Submit the pending structured input first");
    }
    if (!result) return Invalid("Missing client tool result");
    const auto output = OutputJson(*result);
    model_message =
        Object({{"type", String("function_call_output")},
                {"call_id", String(last.GetString("call_id"))},
                {"output", result->IsString() ? *result : String(output)}});
    Set(&last, "output", String(output));
    Set(&last, "status", String("completed"));
    *user = last;
  } else {
    if (waiting)
      return Error(409, "chatkit_tool_pending",
                   "Submit the pending client tool result first");
    const auto id = NewObjectId(ObjectKind::kConversationItem);
    if (id.empty())
      return Error(503, "chatkit_id_unavailable", "No message ID");
    auto prepared = UserInput(objects, access, grant, *input, id, write.id, now,
                              user, &model_message);
    if (!prepared.ok()) return prepared;
    items.push_back(*user);
  }
  StateCipher cipher(objects);
  std::string plain;
  auto opened = cipher.Open(
      grant.data.GetString("workflow_snapshot"),
      {"q4t.chatkit", "workflow", "snapshot", grant.session}, access, &plain);
  if (!opened.ok()) return opened;
  Json definition;
  auto parsed = io::ParseJson(plain, &definition, true, kApplicationJsonLimits);
  CryptoWipe(&plain);
  if (!parsed || definition.GetString("id") != workflow.GetString("id"))
    return Error(503, "workflow_record_invalid", "Invalid workflow snapshot");
  if (const auto* metadata = definition.Find("metadata")) {
    const auto tool = metadata->GetString("q4t_chatkit_structured_input_tool");
    const auto* tools = definition.GetArray("tools");
    if (!tool.empty() && tools &&
        std::ranges::any_of(tools->array, [&](const Json& item) {
          return item.GetString("type") == "function" &&
                 item.GetString("name") == tool;
        })) {
      *form_tool = tool;
      Set(&write.data, "pending_form_tool", String(tool));
    }
  }
  auto request = Object({{"model", String(definition.GetString("model"))},
                         {"background", Boolean(true)},
                         {"store", Boolean(true)},
                         {"input", Array({std::move(model_message)})}});
  for (const auto* key : {"instructions", "reasoning", "text", "tools"})
    if (const auto* v = definition.Find(key); v && !v->IsNull())
      Set(&request, key, *v);
  if (const auto* vars = workflow.Find("state_variables");
      vars && !vars->object.empty()) {
    auto instructions = request.GetString("instructions");
    instructions += "\nWorkflow state variables (JSON)\n" + OutputJson(*vars);
    Set(&request, "instructions", String(std::move(instructions)));
  }
  // The browser submits a result for the latest call without a call ID.
  // Request serial function calls so each result has exactly one recipient.
  Set(&request, "parallel_tool_calls", Boolean(false));
  const Json empty;
  const auto* supplied_options =
      retry ? user->Find("inference_options") :
      input ? input->Find("inference_options") : nullptr;
  const auto& supplied = supplied_options && !structured && !tool_output
                             ? *supplied_options
                             : empty;
  if (const auto* model = supplied.Find("model");
      model && !model->IsNull() &&
      (!model->IsString() || model->str != definition.GetString("model")))
    return Invalid("Model is outside the session workflow");
  if (const auto* tool = supplied.Find("tool_choice"); tool && !tool->IsNull())
    Set(&request, "tool_choice",
        Object({{"type", String("function")},
                {"name", String(tool->GetString("id"))}}));
  if (const auto previous = write.data.GetString("last_response");
      !retry && !previous.empty())
    Set(&request, "previous_response_id", String(previous));
  if (create && !input->GetArray("content")->array.empty() &&
      features->Find("automatic_thread_titling")->GetBool("enabled")) {
    const auto title =
        input->GetArray("content")->array.front().GetString("text");
    size_t bytes = std::min<size_t>(title.size(), 120);
    while (bytes < title.size() && bytes &&
           (static_cast<unsigned char>(title[bytes]) & 0xc0) == 0x80)
      --bytes;
    Set(&Field(write.data, "thread"), "title", String(title.substr(0, bytes)));
  }
  auto run = std::make_unique<ResponsesExecution>(objects, &jobs, true);
  auto result = run->Prepare(request, config, access, now, true, nullptr,
                             nullptr, retry ? &prefix : nullptr);
  if (!result.ok()) return result;
  Set(&write.data, "pending_response", String(run->Id()));
  *thread_id = write.id;
  *response_id = run->Id();
  result = run->BindChatKitThread(std::move(write));
  if (!result.ok()) return result;
  auto* http = detail::HttpResponseContext::Current(fd);
  return jobs.Submit(
      std::move(run), http->Client(),
      [&](ClientLease& lease) { return http->TransferClientLease(lease); },
      http->TakeBatchRate());
}

Json StreamEvent(const Json& event, const std::string& thread,
                 int64_t created) {
  const auto type = event.GetString("type");
  if (type == "response.output_item.added") {
    const auto* item = event.Find("item");
    if (!item || item->GetString("type") != "message" ||
        item->GetString("role") != "assistant")
      return {};
    auto start = Object({{"id", String(item->GetString("id"))},
                         {"thread_id", String(thread)},
                         {"type", String("assistant_message")},
                         {"created_at", Number(created)},
                         {"content", Array()}});
    return Object({{"type", String("thread.item.added")},
                   {"item", chatkit_detail::ClientItem(std::move(start))}});
  }
  auto update =
      Object({{"content_index", Number(event.GetInt("content_index"))}});
  if (type == "response.output_text.delta" ||
      type == "response.refusal.delta") {
    Set(&update, "type", String("assistant_message.content_part.text_delta"));
    Set(&update, "delta", String(event.GetString("delta")));
  } else if (type == "response.content_part.added" ||
             type == "response.content_part.done") {
    const auto* part = event.Find("part");
    if (!part) return {};
    const bool refusal = part->GetString("type") == "refusal";
    if (!refusal && part->GetString("type") != "output_text") return {};
    Set(&update, "type",
        String(type.ends_with("added")
                   ? "assistant_message.content_part.added"
                   : "assistant_message.content_part.done"));
    Set(&update, "content",
        Object(
            {{"type", String("output_text")},
             {"annotations", Array()},
             {"text", String(part->GetString(refusal ? "refusal" : "text"))}}));
  } else
    return {};
  return Object({{"type", String("thread.item.updated")},
                 {"item_id", String(event.GetString("item_id"))},
                 {"update", std::move(update)}});
}

void Fail(int fd, const StateAccess& access, const ObjectReply& error) {
  detail::SendError(fd, error.http_status, error.ClientMessage(),
                    {access.deadline}, error.ClientCode(),
                    error.value.GetString("param"));
}
bool Emit(int fd, const Json& event, const StateAccess& access) {
  return detail::WriteAll(fd, "data: " + OutputJson(event) + "\r\n\r\n",
                          {access.deadline});
}
}  // namespace

void ServeChatKitClient(int fd, ObjectStore& objects, ResponsesJobs* jobs,
                        const ChatPlanConfig& config, const StateAccess& access,
                        std::string_view token, const std::string& body,
                        ChatKitIssuer issuer, std::string_view origin,
                        const std::function<void(const Json&)>& transcribe) {
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Allowed()) {
    Fail(fd, access, Error(401, "invalid_client_secret", "Invalid session"));
    return;
  }
  Grant grant;
  auto authorized = chatkit_detail::Redeem(objects, access, token,
                                           std::time(nullptr), &grant);
  if (!authorized.ok()) {
    Fail(fd, access, authorized);
    return;
  }
  Json input;
  if (!io::ParseJson(body, &input, true, kApplicationJsonLimits) ||
      !input.Find("params") || !input.Find("params")->IsObject()) {
    Fail(fd, access, Invalid("Expected a ChatKit request with params"));
    return;
  }
  const auto& params = *input.Find("params");
  const auto type = input.GetString("type");
  if (type == "input.transcribe") {
    transcribe(params);
    return;
  }
  const bool create = type == "threads.create";
  const bool retry = type == "threads.retry_after_item";
  if (!create && !retry && type != "threads.add_structured_input" &&
      type != "threads.add_user_message" &&
      type != "threads.add_client_tool_output") {
    auto result = type == "attachments.create"
                      ? chatkit_detail::ReserveAttachment(objects, access, grant,
                                                         params, issuer, origin)
                      : chatkit_detail::BrowserOperation(objects, access, grant, type, params);
    if (!result.ok()) {
      Fail(fd, access, result);
      return;
    }
    detail::SendSimple(fd, 200, "OK", OutputJson(result.value),
                       "application/json", {access.deadline}, {}, {},
                       type == "attachments.create");
    return;
  }
  if (!jobs) {
    Fail(fd, access,
         Error(503, "background_unavailable", "Session executor unavailable"));
    return;
  }
  Json user;
  std::string id, response_id, form_tool;
  auto submitted = Submit(fd, objects, *jobs, config, access, grant, params,
                          type, &id, &response_id, &form_tool, &user);
  if (!submitted.ok()) {
    Fail(fd, access, submitted);
    return;
  }
  if (retry && response_id.empty()) {
    if (detail::BeginStreamingResponse(fd, http->State(), {access.deadline})) {
      http->BeforeTerminal();
      *http->State() = detail::ResponseState::kComplete;
    }
    return;
  }
  auto row = chatkit_detail::ReadThread(objects, access, id, &grant);
  if (!row.ok()) {
    Fail(fd, access, row);
    return;
  }
  if (!detail::BeginStreamingResponse(fd, http->State(), {access.deadline}))
    return;
  if (create) {
    auto thread = chatkit_detail::ClientThread(Field(row.value, "data"));
    Set(&thread, "items",
        Object(
            {{"data", Array()}, {"after", {}}, {"has_more", Boolean(false)}}));
    if (!Emit(fd,
              Object({{"type", String("thread.created")},
                      {"thread", std::move(thread)}}),
              access))
      return;
  }
  const auto done = [&](const Json& item) {
    return Emit(fd,
                Object({{"type", String("thread.item.done")},
                        {"item", chatkit_detail::ClientItem(item)}}),
                access);
  };
  if (type == "threads.add_client_tool_output" ||
      type == "threads.add_structured_input") {
    if (!Emit(fd,
              Object({{"type", String("thread.item.replaced")},
                      {"item", chatkit_detail::ClientItem(user)}}),
              access))
      return;
  } else if (!retry && !done(user))
    return;
  size_t next = 0;
  ResponsesState state(objects);
  auto heartbeat = Clock::now() + std::chrono::seconds(5);
  while (Clock::now() < access.deadline &&
         (!access.cancelled || !access.cancelled())) {
    // A newer user turn may already be running. Follow only this admission's
    // original Response journal, including its terminal outcome.
    auto record = state.ReadRecord(response_id, access);
    if (!record.ok()) {
      Emit(fd,
           Object({{"type", String("error")},
                   {"code", String("custom")},
                   {"message", String(record.ClientMessage())},
                   {"allow_retry", Boolean(true)}}),
           access);
      return;
    }
    const auto& response = *record.value.Find("response");
    const auto& events = record.value.GetArray("events")->array;
    while (next < events.size()) {
      auto wire =
          StreamEvent(events[next++], id, response.GetInt("created_at"));
      if (!wire.IsNull() && !Emit(fd, wire, access)) return;
    }
    const auto status = response.GetString("status");
    if (status != "queued" && status != "in_progress") {
      row = chatkit_detail::ReadThread(objects, access, id, &grant);
      if (!row.ok()) {
        Emit(fd,
             Object({{"type", String("error")},
                     {"code", String("custom")},
                     {"message", String(row.ClientMessage())},
                     {"allow_retry", Boolean(true)}}),
             access);
        return;
      }
      auto projected = chatkit_detail::ProjectOutput(response, id, form_tool);
      if (!projected.ok()) return;
      for (const auto& item : projected.value.array)
        if (!done(item)) return;
      if (status != "completed" &&
          !Emit(fd,
                Object({{"type", String("error")},
                        {"code", String("custom")},
                        {"message",
                         String("Model response ended before completion")},
                        {"allow_retry", Boolean(true)}}),
                access))
        return;
      http->BeforeTerminal();
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
}
}  // namespace q4t::server
