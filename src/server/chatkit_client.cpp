#include "q4t/server/chatkit.h"

#include <algorithm>
#include <ctime>
#include <thread>

#include "chatkit_credentials.h"
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

ObjectReply UserInput(const Json& input, const std::string& id,
                      const std::string& thread_id, int64_t now,
                      Json* user, Json* model_message) {
  const auto* parts = input.GetArray("content");
  const auto* attachments = input.GetArray("attachments");
  const auto* options = input.Find("inference_options");
  if (!parts || parts->array.empty() || !attachments || !options || !options->IsObject())
    return Invalid("Expected message content, attachments and inference options");
  if (!attachments->array.empty())
    return Invalid("Expected an attachment uploaded for this ChatKit user");
  Json content = Array(), model = Array();
  if (const auto* quote = input.Find("quoted_text"); quote && !quote->IsNull()) {
    if (!quote->IsString()) return Invalid("Expected quoted text");
    content.array.push_back(Object({{"type", String("quoted_text")}, {"text", *quote}}));
    model.array.push_back(Object({{"type", String("input_text")}, {"text", *quote}}));
  }
  for (const auto& part : parts->array) {
    const auto* text = part.Find("text");
    if (part.GetString("type") != "input_text" || !text || !text->IsString())
      return Invalid("Expected a ChatKit text part");
    auto wire = Object({{"type", String("input_text")}, {"text", *text}});
    content.array.push_back(wire);
    model.array.push_back(std::move(wire));
  }
  *user = Object({{"id", String(id)}, {"thread_id", String(thread_id)},
                  {"type", String("chatkit.user_message")},
                  {"object", String("chatkit.thread_item")}, {"created_at", Number(now)},
                  {"content", std::move(content)}, {"attachments", Array()},
                  {"inference_options", options->object.empty() ? Json{} :
                      Object({{"model", options->Has("model") ? *options->Find("model") : Json{}},
                              {"tool_choice", options->Has("tool_choice") ? *options->Find("tool_choice") : Json{}}})}});
  *model_message = Object({{"role", String("user")}, {"content", std::move(model)}});
  return {};
}

ObjectReply Submit(int fd, ObjectStore& objects, ResponsesJobs& jobs,
                   const ChatPlanConfig& config, const StateAccess& access,
                   const Grant& grant, const Json& params, bool create,
                   std::string* thread_id, Json* user) {
  const auto& session = *grant.data.Find("session");
  const auto& workflow = *session.Find("workflow");
  const auto* features = session.Find("chatkit_configuration");
  const auto* input = params.Find("input");
  if (!input || !input->IsObject()) return Invalid("Missing user message");
  ObjectRequest write;
  write.kind = ObjectKind::kChatKitThread;
  write.operation = ObjectOperation::kPut;
  write.scope = access.scope;
  write.id = create ? NewObjectId(write.kind) : params.GetString("thread_id");
  if (!write.id.starts_with("cthr_") || !ValidApplicationId(write.id))
    return Invalid("Expected a thread identifier");
  const auto now = std::time(nullptr);
  if (create) {
    auto thread = Object({{"id", String(write.id)}, {"object", String("chatkit.thread")},
                          {"created_at", Number(now)}, {"title", {}},
                          {"status", Object({{"type", String("active")}})},
                          {"user", String(session.GetString("user"))}});
    write.data = Object({{"thread", std::move(thread)}, {"items", Array()},
                         {"workflow", String(workflow.GetString("id"))},
                         {"created_by", String(grant.session)}});
    const bool history = features->Find("history")->GetBool("enabled");
    write.ttl_seconds = history ? 0 : std::max<int64_t>(1, session.GetInt("expires_at") - now);
  } else {
    auto old = chatkit_detail::ReadThread(objects, access, write.id, session.GetString("user"));
    if (!old.ok()) return old;
    write.expected_revision = old.value.GetInt("revision");
    write.expires_at = old.value.GetInt("expires_at");
    write.data = Field(old.value, "data");
    if (write.data.GetString("workflow") != workflow.GetString("id") ||
        (!features->Find("history")->GetBool("enabled") &&
         write.data.GetString("created_by") != grant.session))
      return Error(404, "object_not_found", "Thread not found");
    if (!write.data.GetString("pending_response").empty())
      return Error(409, "chatkit_thread_busy", "A thread response is in progress");
  }
  const auto id = NewObjectId(ObjectKind::kConversationItem);
  if (id.empty()) return Error(503, "chatkit_id_unavailable", "No message ID");
  Json model_message;
  auto prepared = UserInput(*input, id, write.id, now, user, &model_message);
  if (!prepared.ok()) return prepared;
  StateCipher cipher(objects);
  std::string plain;
  auto opened = cipher.Open(grant.data.GetString("workflow_snapshot"),
                            {"q4t.chatkit", "workflow", "snapshot", grant.session}, access, &plain);
  if (!opened.ok()) return opened;
  Json definition;
  auto parsed = io::ParseJson(plain, &definition, true, kApplicationJsonLimits);
  CryptoWipe(&plain);
  if (!parsed || definition.GetString("id") != workflow.GetString("id"))
    return Error(503, "workflow_record_invalid", "Invalid workflow snapshot");
  auto request = Object({{"model", String(definition.GetString("model"))},
                         {"background", Boolean(true)}, {"store", Boolean(true)},
                         {"input", Array({std::move(model_message)})}});
  for (const auto* key : {"instructions", "reasoning", "text", "tools"})
    if (const auto* v = definition.Find(key); v && !v->IsNull())
      Set(&request, key, *v);
  if (const auto* vars = workflow.Find("state_variables"); vars && !vars->object.empty()) {
    auto instructions = request.GetString("instructions");
    instructions += "\nWorkflow state variables (JSON)\n" + OutputJson(*vars);
    Set(&request, "instructions", String(std::move(instructions)));
  }
  const auto& supplied = *input->Find("inference_options");
  if (const auto* model = supplied.Find("model"); model && !model->IsNull() &&
      (!model->IsString() || model->str != definition.GetString("model")))
    return Invalid("Model is outside the session workflow");
  if (const auto* tool = supplied.Find("tool_choice"); tool && !tool->IsNull())
    Set(&request, "tool_choice", Object({{"type", String("function")},
                                        {"name", String(tool->GetString("id"))}}));
  if (const auto previous = write.data.GetString("last_response"); !previous.empty())
    Set(&request, "previous_response_id", String(previous));
  if (create && features->Find("automatic_thread_titling")->GetBool("enabled")) {
    const auto title = input->GetArray("content")->array.front().GetString("text");
    size_t bytes = std::min<size_t>(title.size(), 120);
    while (bytes < title.size() && bytes && (static_cast<unsigned char>(title[bytes]) & 0xc0) == 0x80)
      --bytes;
    Set(&Field(write.data, "thread"), "title", String(title.substr(0, bytes)));
  }
  auto run = std::make_unique<ResponsesExecution>(objects, &jobs, true);
  auto result = run->Prepare(request, config, access, now, true);
  if (!result.ok()) return result;
  Field(write.data, "items").array.push_back(*user);
  Set(&write.data, "pending_response", String(run->Id()));
  *thread_id = write.id;
  result = run->BindChatKitThread(std::move(write));
  if (!result.ok()) return result;
  auto* http = detail::HttpResponseContext::Current(fd);
  return jobs.Submit(std::move(run), http->Client(),
                     [&](ClientLease& lease) { return http->TransferClientLease(lease); },
                     http->TakeBatchRate());
}

void Fail(int fd, const StateAccess& access, const ObjectReply& error) {
  detail::SendError(fd, error.http_status, error.ClientMessage(), {access.deadline},
                    error.ClientCode(), error.value.GetString("param"));
}
bool Emit(int fd, const Json& event, const StateAccess& access) {
  return detail::WriteAll(fd, "data: " + OutputJson(event) + "\r\n\r\n", {access.deadline});
}
}  // namespace

void ServeChatKitClient(int fd, ObjectStore& objects, ResponsesJobs* jobs,
                       const ChatPlanConfig& config, const StateAccess& access,
                       std::string_view token, const std::string& body) {
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Allowed()) {
    Fail(fd, access, Error(401, "invalid_client_secret", "Invalid session"));
    return;
  }
  Grant grant;
  auto authorized = chatkit_detail::Redeem(objects, access, token, std::time(nullptr), &grant);
  if (!authorized.ok()) { Fail(fd, access, authorized); return; }
  Json input;
  if (!io::ParseJson(body, &input, true, kApplicationJsonLimits) ||
      !input.Find("params") || !input.Find("params")->IsObject()) {
    Fail(fd, access, Invalid("Expected a ChatKit request with params"));
    return;
  }
  const auto& params = *input.Find("params");
  const auto type = input.GetString("type");
  const auto owner = Field(grant.data, "session").GetString("user");
  if (type == "threads.get_by_id") {
    auto found = chatkit_detail::ReadThread(objects, access, params.GetString("thread_id"), owner);
    if (!found.ok()) { Fail(fd, access, found); return; }
    auto data = chatkit_detail::ClientThread(Field(found.value, "data"));
    detail::SendSimple(fd, 200, "OK", OutputJson(data), "application/json", {access.deadline});
    return;
  }
  const bool create = type == "threads.create";
  if (!create && type != "threads.add_user_message") {
    Fail(fd, access, Invalid("Unknown ChatKit client operation"));
    return;
  }
  if (!jobs) {
    Fail(fd, access, Error(503, "background_unavailable", "Session executor unavailable"));
    return;
  }
  Json user;
  std::string id;
  auto submitted = Submit(fd, objects, *jobs, config, access, grant, params, create, &id, &user);
  if (!submitted.ok()) { Fail(fd, access, submitted); return; }
  auto row = chatkit_detail::ReadThread(objects, access, id, owner);
  if (!row.ok()) { Fail(fd, access, row); return; }
  if (!detail::BeginStreamingResponse(fd, http->State(), {access.deadline})) return;
  if (create) {
    auto thread = chatkit_detail::ClientThread(Field(row.value, "data"));
    Set(&thread, "items", Object({{"data", Array()}, {"after", {}}, {"has_more", Boolean(false)}}));
    if (!Emit(fd, Object({{"type", String("thread.created")}, {"thread", std::move(thread)}}), access)) return;
  }
  const auto done = [&](const Json& item) {
    return Emit(fd, Object({{"type", String("thread.item.done")},
                            {"item", chatkit_detail::ClientItem(item)}}), access);
  };
  if (!done(user)) return;
  while (Clock::now() < access.deadline && !access.cancelled()) {
    const auto& data = Field(row.value, "data");
    if (data.GetString("pending_response").empty()) {
      bool start = false;
      for (const auto& item : data.GetArray("items")->array) {
        if (item.GetString("id") == user.GetString("id")) { start = true; continue; }
        if (start && item.GetString("type") == "chatkit.user_message") break;
        if (start && !done(item)) return;
      }
      if (data.GetString("last_status") != "completed" &&
          !Emit(fd, Object({{"type", String("error")}, {"code", String("custom")},
                            {"message", String("Model response ended before completion")},
                            {"allow_retry", Boolean(true)}}), access)) return;
      http->BeforeTerminal();
      *http->State() = detail::ResponseState::kComplete;
      return;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    row = chatkit_detail::ReadThread(objects, access, id, owner);
    if (!row.ok()) return;
  }
}
}  // namespace q4t::server
