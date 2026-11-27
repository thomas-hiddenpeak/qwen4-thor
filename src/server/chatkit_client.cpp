#include "q4t/server/chatkit.h"

#include <algorithm>
#include <ctime>
#include <thread>

#include "chatkit_credentials.h"
#include "q4t/server/lifetime.h"

#include <cstdio>
#include "chatkit_actions.h"
#include "chatkit_generation_stream.h"
#include "chatkit_retry.h"
#include "chatkit_structured.h"
#include "chatkit_attachments.h"
#include "chatkit_thread_state.h"
#include "chatkit_workflow.h"
#include "object_protocol.h"
#include "q4t/server/agent_registration.h"
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
                   std::string* response_id, std::string* form_tool, Json* user,
                   Json* previous_workflow) {
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
  // Workflow widgets can be appended between a model call and its result.
  // The latest conversation item owns admission of the next model turn.
  auto latest =
      std::find_if(items.rbegin(), items.rend(), [](const Json& item) {
        const auto type = item.GetString("type");
        return type != "chatkit.widget" && type != "chatkit.task_group";
      });
  const bool waiting =
      latest != items.rend() &&
      latest->GetString("type") == "chatkit.client_tool_call" &&
      latest->GetString("status") == "in_progress";
  Json model_message;
  chatkit_detail::RetryHistory prefix;
  if (retry) {
    ResponsesState state(objects);
    auto record = state.ReadRecord(write.data.GetString("last_response"), access);
    if (!record.ok()) return record;
    auto selected = chatkit_detail::SelectRetryHistory(
        record.value, params.GetString("item_id"), &write.data, user, &prefix);
    if (!selected.ok() || prefix.items.IsNull()) return selected;
    // The reference binds the original admitted item, including inputs
    // retained only in the interrupted thread's Response journal.
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
      if (!waiting || &*it != &*latest ||
          it->GetString("status") != "in_progress")
        return Error(409, "chatkit_input_not_pending", "Input is not pending");
    }
    if (!waiting)
      return Error(409, "chatkit_no_pending_tool", "No pending client tool");
    auto& last = *latest;
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
    const auto id = NewObjectId(ObjectKind::kConversationItem);
    if (id.empty())
      return Error(503, "chatkit_id_unavailable", "No message ID");
    auto prepared = UserInput(objects, access, grant, *input, id, write.id, now,
                              user, &model_message);
    if (!prepared.ok()) return prepared;
    items.push_back(*user);
  }
  Json definition;
  auto opened =
      chatkit_detail::OpenWorkflow(objects, access, grant, &definition);
  if (!opened.ok()) return opened;
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
  for (const auto* key : {"instructions", "reasoning", "text"})
    if (const auto* v = definition.Find(key); v && !v->IsNull())
      Set(&request, key, *v);
  if (const auto* registered = definition.GetArray("tools")) {
    auto tools = Array();
    for (const auto& tool : registered->array) {
      auto native = tool.GetString("type") == "web_search"
                        ? NativeAgentWebSearch(tool)
                        : tool;
      if (!native.IsNull()) tools.array.push_back(std::move(native));
    }
    Set(&request, "tools", std::move(tools));
  }
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
  if (const auto* choice = supplied.Find("tool_choice");
      choice && !choice->IsNull()) {
    const auto id = choice->GetString("id");
    const auto* tools = request.GetArray("tools");
    auto selector =
        Object({{"type", String("function")}, {"name", String(id)}});
    if (id == "web_search" && tools) {
      const auto named = [&](const Json& tool) {
        return tool.GetString("type") == "function" &&
               tool.GetString("name") == id;
      };
      const auto web = [](const Json& tool) {
        return tool.GetString("type") == "web_search";
      };
      if (!std::ranges::any_of(tools->array, named) &&
          std::ranges::any_of(tools->array, web))
        selector = Object({{"type", String("web_search")}});
    }
    Set(&request, "tool_choice", std::move(selector));
  }
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
  if (const auto* previous =
          chatkit_detail::ResumableWorkflow(Field(write.data, "items"))) {
    *previous_workflow = *previous;
    Set(&write.data, "pending_workflow_id", *previous->Find("id"));
  }
  auto run = std::make_unique<ResponsesExecution>(objects, &jobs, true);
  auto result = run->Prepare(request, config, access, now, true, nullptr,
                             nullptr, retry ? &prefix : nullptr, write.id);
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

Json chatkit_detail::GenerationStream::MessageEvent(const Json& event,
                                                    const std::string& thread,
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
    auto content = AssistantContent(*part);
    if (content.IsNull()) return {};
    Set(&update, "type",
        String(type.ends_with("added")
                   ? "assistant_message.content_part.added"
                   : "assistant_message.content_part.done"));
    Set(&update, "content", std::move(content));
  } else if (type == "response.output_text.annotation.added") {
    const auto* raw = event.Find("annotation");
    if (!raw) return {};
    auto ref = BrowserAnnotation(*raw);
    if (ref.IsNull()) return {};
    auto& next = annotation_counts_[{event.GetString("item_id"),
                                     event.GetInt("content_index")}];
    Set(&update, "type",
        String("assistant_message.content_part.annotation_added"));
    Set(&update, "annotation_index", Number(next++));
    Set(&update, "annotation", std::move(ref));
  } else
    return {};
  return Object({{"type", String("thread.item.updated")},
                 {"item_id", String(event.GetString("item_id"))},
                 {"update", std::move(update)}});
}

bool chatkit_detail::GenerationStream::EmitEvent(const Json& event,
                                                 const std::string& thread,
                                                 int64_t created,
                                                 const Json* times,
                                                 const Emitter& emit) {
  const auto type = event.GetString("type");
  if (type == "response.output_item.added") {
    const auto* item = event.Find("item");
    if (!item) return true;
    const auto kind = item->GetString("type");
    const auto at = WorkflowItemTime(times, item->GetString("id"));
    if (kind == "reasoning") {
      const auto id = item->GetString("id");
      if (id.empty()) return true;
      reasoning_ids_.insert(id);
      if (!workflow_.IsNull()) return true;
      workflow_ = NewWorkflow(*item, thread, at ? at : created);
      return emit(Object({{"type", String("thread.item.added")},
                          {"item", ClientItem(workflow_)}}));
    }
    if (kind == "message" && !workflow_.IsNull()) {
      CloseWorkflow(workflow_, at);
      if (!emit(Object({{"type", String("thread.item.done")},
                        {"item", ClientItem(std::move(workflow_))}})))
        return false;
      workflow_ = {};
      reasoning_ids_.clear();
      streaming_item_.clear();
    }
  }
  if (type == "response.output_item.done") {
    const auto* item = event.Find("item");
    if (!item || item->GetString("type") != "message") return true;
    const auto response =
        Object({{"created_at", Number(created)}, {"output", Array({*item})}});
    auto projected = ProjectOutput(response, thread);
    if (!projected.ok() || projected.value.array.empty()) return true;
    const auto id = item->GetString("id");
    if (!finished_messages_.insert(id).second) return true;
    return emit(Object(
        {{"type", String("thread.item.done")},
         {"item", ClientItem(std::move(projected.value.array.front()))}}));
  }
  const bool delta = type == "response.reasoning_summary_text.delta";
  if (delta || type == "response.reasoning_summary_text.done") {
    if (workflow_.IsNull()) return true;
    const auto item = event.GetString("item_id");
    const auto index = event.GetInt("summary_index", -1);
    if (!reasoning_ids_.contains(item) || index < 0) return true;
    auto& tasks = WorkflowTasks(workflow_).array;
    const bool streaming = !streaming_item_.empty() &&
                           item == streaming_item_ && index == streaming_index_;
    if (delta && !streaming && !tasks.empty()) return true;
    const auto* text = event.Find(delta ? "delta" : "text");
    if (!text || !text->IsString()) return true;
    size_t task_index = 0;
    if (streaming) {
      auto& content = Field(tasks.front(), "content").str;
      if (delta)
        content.append(text->str);
      else
        content = text->str;
    } else {
      task_index = tasks.size();
      tasks.push_back(PublicThought(text->str));
    }
    if (delta) {
      streaming_item_ = item;
      streaming_index_ = index;
    } else if (streaming) {
      streaming_item_.clear();
    }
    return emit(Object({
        {"type", String("thread.item.updated")},
        {"item_id", *workflow_.Find("id")},
        {"update", Object({{"type", String(streaming ? "workflow.task.updated"
                                                     : "workflow.task.added")},
                           {"task_index", Number(task_index)},
                           {"task", tasks[task_index]}})},
    }));
  }
  auto message = MessageEvent(event, thread, created);
  return message.IsNull() || emit(message);
}

void chatkit_detail::FinishStreamError(int fd, const StateAccess& access,
                                       const ObjectReply& error,
                                       bool allow_retry) {
  auto* http = detail::HttpResponseContext::Current(fd);
  const auto message = error.ClientMessage();
  http->ObserveError(error.http_status, message, error.ClientCode());
  http->BeforeTerminal();
  *http->State() = detail::ResponseState::kTerminal;
  if (Emit(fd,
           Object({{"type", String("error")},
                   {"code", String("custom")},
                   {"message", String(message)},
                   {"allow_retry", Boolean(allow_retry)}}),
           access))
    *http->State() = detail::ResponseState::kComplete;
}

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
  http->SetChatKitStream();
  const bool stream_action = type == "threads.custom_action";
  if (stream_action || type == "threads.sync_custom_action") {
    bool started = false;
    const auto begin = [&](bool options, const StateAccess& write_access) {
      if (started) return true;
      if (!detail::BeginStreamingResponse(fd, http->State(),
                                          {write_access.deadline}))
        return false;
      started = true;
      return !options ||
             Emit(fd,
                  Object({{"type", String("stream_options")},
                          {"stream_options",
                           Object({{"allow_cancel", Boolean(true)}})}}),
                  write_access);
    };
    std::function<bool(const Json&)> emit_event;
    if (stream_action)
      emit_event = [&](const Json& event) {
        auto write_access = access;
        write_access.deadline =
            std::min(access.deadline, Clock::now() + std::chrono::seconds(2));
        return begin(true, write_access) && Emit(fd, event, write_access);
      };
    auto result = chatkit_detail::DispatchAction(objects, access, grant, params,
                                                 stream_action, emit_event);
    if (!result.ok()) {
      if (!started) {
        Fail(fd, access, result);
        return;
      }
      // Once provisional progress is on the wire the status line is final.
      // A failed action never sends a success item or a second HTTP response.
      chatkit_detail::FinishStreamError(fd, access, result,
                                        result.http_status == 409);
      return;
    } else if (stream_action) {
      const auto& events = result.value.array;
      const bool entered =
          events.empty() || events.front().GetString("type") != "error";
      if (!begin(entered, access)) return;
      for (const auto& event : events)
        if (!Emit(fd, event, access)) return;
    } else {
      detail::SendSimple(fd, 200, "OK", OutputJson(result.value),
                         "application/json", {access.deadline});
      return;
    }
    http->BeforeTerminal();
    *http->State() = detail::ResponseState::kComplete;
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
  Json user, previous_workflow;
  std::string id, response_id, form_tool;
  auto submitted =
      Submit(fd, objects, *jobs, config, access, grant, params, type, &id,
             &response_id, &form_tool, &user, &previous_workflow);
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
  // Submit transferred this admission to a durable Responses job. A closed
  // browser socket cancels only that job; an ordinary background Responses
  // request or a later turn on the same thread has a different owner/id.
  ScopeExit cancel_on_disconnect([&]() noexcept {
    if (!detail::ClientConnectionFailed(fd)) return;
    try {
      jobs->RequestCancel(access.scope, response_id);
    } catch (...) {
      std::fputs("[q4t][chatkit] failed to signal disconnected generation\n",
                 stderr);
    }
  });
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
  if (!Emit(fd,
            Object({{"type", String("stream_options")},
                    {"stream_options",
                     Object({{"allow_cancel", Boolean(true)}})}}),
            access))
    return;
  chatkit_detail::GenerationStream stream(previous_workflow);
  const auto send = [&](const Json& event) { return Emit(fd, event, access); };
  size_t next = 0;
  ResponsesState state(objects);
  auto heartbeat = Clock::now() + std::chrono::seconds(5);
  while (Clock::now() < access.deadline &&
         (!access.cancelled || !access.cancelled())) {
    // A newer user turn may already be running. Follow only this admission's
    // original Response journal, including its terminal outcome.
    auto record = state.ReadRecord(response_id, access);
    if (!record.ok()) {
      chatkit_detail::FinishStreamError(fd, access, record, true);
      return;
    }
    const auto& response = *record.value.Find("response");
    const auto& events = record.value.GetArray("events")->array;
    while (next < events.size()) {
      if (!stream.EmitEvent(events[next++], id, response.GetInt("created_at"),
                            record.value.Find("chatkit_item_times"), send))
        return;
    }
    const auto status = response.GetString("status");
    if (status != "queued" && status != "in_progress") {
      row = chatkit_detail::ReadThread(objects, access, id, &grant);
      if (!row.ok()) {
        chatkit_detail::FinishStreamError(fd, access, row, true);
        return;
      }
      auto projected = chatkit_detail::ProjectOutput(
          response, id, form_tool, record.value.Find("chatkit_item_times"),
          previous_workflow.IsNull() ? nullptr : &previous_workflow);
      if (!projected.ok()) {
        chatkit_detail::FinishStreamError(fd, access, projected, true);
        return;
      }
      for (const auto& item : projected.value.array)
        if (item.GetString("type") != "chatkit.task_group" &&
            !stream.FinishedMessage(item.GetString("id")) && !done(item))
          return;
      if (status != "completed") {
        const auto error = Error(502, "chatkit_generation_failed",
                                 "Model response ended before completion");
        chatkit_detail::FinishStreamError(fd, access, error, true);
        return;
      }
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
