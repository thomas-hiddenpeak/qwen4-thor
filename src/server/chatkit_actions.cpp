#include "chatkit_actions.h"

#include <algorithm>
#include <ctime>

#include "chatkit_thread_state.h"
#include "object_protocol.h"
#include "q4t/server/files.h"
#include "q4t/server/program_worker.h"
#include "q4t/server/request_json.h"

namespace q4t::server::chatkit_detail {
namespace {
using namespace object_detail;
constexpr size_t kActionFileBytes = 512 * 1024;
constexpr size_t kWorkerCodeBytes = 65536;

ObjectReply Invalid(std::string field, std::string message) {
  auto error = Error(400, "invalid_request", std::move(message));
  error.value = Object({{"param", String(std::move(field))}});
  return error;
}

ObjectReply BadOutput() {
  return Error(502, "chatkit_action_invalid_result",
               "The workflow action returned an invalid result");
}

ObjectReply Execute(const StateAccess& access, const Json& code, Json context,
                    int64_t now,
                    const std::function<bool(std::string_view)>& progress) {
  auto input = Object({{"code", code},
                       {"action_context", std::move(context)},
                       {"tools", Array()},
                       {"calls", Array()},
                       {"results", Array()},
                       {"seed", Number(0)},
                       {"epoch_ms", Number(now * 1000)}});
  auto run = ExecuteActionWorker(
      input, {access.scope, access.deadline, access.cancelled}, progress);
  if (!run.ok()) return run;
  const auto* output = run.value.GetArray("output");
  if (run.value.GetString("status") != "completed" || !output ||
      output->array.size() != 1 ||
      output->array.front().GetString("type") != "text")
    return BadOutput();
  Json result;
  if (!io::ParseJson(output->array.front().GetString("value"), &result, true,
                     kApplicationJsonLimits) ||
      !result.IsObject())
    return BadOutput();
  for (const auto& [key, value] : result.object)
    if (key != "state" && key != "widget" && key != "effects" &&
        key != "progress" && key != "notices" && key != "title" &&
        key != "remove_sender")
      return BadOutput();
  run.value = std::move(result);
  return run;
}

bool WidgetTree(const Json& node) {
  if (!node.IsObject() || node.GetString("type").empty()) return false;
  const auto* children = node.Find("children");
  if (!children || children->IsNull()) return true;
  if (children->IsObject()) return WidgetTree(*children);
  return children->IsArray() &&
         std::ranges::all_of(children->array, WidgetTree);
}

ObjectReply PrepareEvents(const Json& result, bool streaming, Json* events) {
  if (const auto* progress = result.Find("progress")) {
    if (!streaming || !progress->IsArray()) return BadOutput();
    for (const auto& text : progress->array) {
      if (!text.IsString()) return BadOutput();
      events->array.push_back(
          Object({{"type", String("progress_update")}, {"text", text}}));
    }
  }
  if (const auto* notices = result.Find("notices")) {
    if (!streaming || !notices->IsArray()) return BadOutput();
    for (const auto& notice : notices->array) {
      const auto level = notice.GetString("level");
      const auto* message = notice.Find("message");
      const auto* title = notice.Find("title");
      if ((level != "info" && level != "warning" && level != "danger") ||
          !message || !message->IsString() ||
          (title && !title->IsString() && !title->IsNull()))
        return BadOutput();
      auto event = Object({{"type", String("notice")},
                           {"level", String(level)},
                           {"message", *message}});
      if (title) Set(&event, "title", *title);
      events->array.push_back(std::move(event));
    }
  }
  if (const auto* effects = result.Find("effects")) {
    if (!streaming || !effects->IsArray()) return BadOutput();
    for (const auto& effect : effects->array) {
      const auto* data = effect.Find("data");
      if (effect.GetString("name").empty() || (data && !data->IsObject()))
        return BadOutput();
      events->array.push_back(Object({{"type", String("client_effect")},
                                      {"name", *effect.Find("name")},
                                      {"data", data ? *data : Object()}}));
    }
  }
  return {};
}
}  // namespace

ObjectReply OpenWorkflow(ObjectStore& objects, const StateAccess& access,
                         const Grant& grant, Json* definition) {
  StateCipher cipher(objects);
  std::string plain;
  auto opened = cipher.Open(
      grant.data.GetString("workflow_snapshot"),
      {"q4t.chatkit", "workflow", "snapshot", grant.session}, access, &plain);
  if (!opened.ok()) return opened;
  auto parsed = io::ParseJson(plain, definition, true, kApplicationJsonLimits);
  CryptoWipe(&plain);
  const auto* session = grant.data.Find("session");
  const auto* workflow = session ? session->Find("workflow") : nullptr;
  if (!parsed || !workflow ||
      definition->GetString("id") != workflow->GetString("id"))
    return Error(503, "workflow_record_invalid", "Invalid workflow snapshot");
  return {};
}

ObjectReply BindActionFile(ObjectStore& objects, const StateAccess& access,
                           Json* workflow) {
  const auto* meta = workflow->Find("metadata");
  const auto* file = meta ? meta->Find("q4t_chatkit_actions_file") : nullptr;
  if (!file) return {};
  if (!file->IsString() || !file->str.starts_with("file_") ||
      !ValidApplicationId(file->str))
    return Invalid("workflow", "Invalid workflow actions file");
  const BinaryAccess owner{access.scope, access.deadline, access.cancelled};
  BinaryRead pin;
  auto opened =
      Files(objects).Open(owner, file->str, &pin, nullptr, kActionFileBytes);
  if (!opened.ok()) return opened;
  BinaryStore store(objects);
  std::string source;
  for (size_t i = 0; i < pin.manifest.chunks.size(); ++i) {
    std::string bytes;
    opened = store.Read(owner, pin, i, &bytes);
    if (!opened.ok()) break;
    source += bytes;
  }
  auto closed = store.Close(owner, &pin);
  if (!opened.ok()) return opened;
  if (!closed.ok()) return closed;
  Json deployment;
  const auto parsed =
      io::ParseJson(source, &deployment, true, kApplicationJsonLimits);
  CryptoWipe(&source);
  const auto* actions = deployment.Find("actions");
  if (!parsed || !actions || !actions->IsObject() || actions->object.empty())
    return Invalid("workflow", "Expected a deployment with named actions");
  for (const auto& [name, code] : actions->object)
    if (name.empty() || !code.IsString() || code.str.empty() ||
        code.str.size() > kWorkerCodeBytes ||
        code.str.find('\0') != std::string::npos)
      return Invalid("workflow", "Invalid action name or handler source");
  Set(workflow, "chatkit_actions", *actions);
  return {};
}

ObjectReply DispatchAction(
    ObjectStore& objects, const StateAccess& access, const Grant& grant,
    const Json& params, bool streaming,
    const std::function<bool(std::string_view)>& progress) {
  const auto id = params.GetString("thread_id");
  const auto* sender_id = params.Find("item_id");
  const auto* action = params.Find("action");
  if (id.empty()) return Invalid("thread_id", "Expected a thread ID");
  if (sender_id && !sender_id->IsNull() && !sender_id->IsString())
    return Invalid("item_id", "Expected a widget ID");
  if (!action || action->GetString("type").empty())
    return Invalid("action.type", "Expected a workflow action name");
  auto row = ReadThread(objects, access, id, &grant);
  if (!row.ok()) return row;
  auto& data = Field(row.value, "data");
  auto& items = Field(data, "items").array;
  Json* sender = nullptr;
  if (sender_id && !sender_id->str.empty()) {
    const auto it = std::ranges::find_if(items, [&](const Json& item) {
      return item.GetString("id") == sender_id->str;
    });
    if (it == items.end())
      return Error(404, "object_not_found", "Widget not found");
    if (it->GetString("type") != "chatkit.widget") {
      if (!streaming) return Invalid("item_id", "Expected a widget sender");
      ObjectReply error_event;
      error_event.value = Array({Object({{"type", String("error")},
                                         {"code", String("stream.error")},
                                         {"allow_retry", Boolean(false)}})});
      return error_event;
    }
    sender = &*it;
  }
  Json workflow;
  auto opened = OpenWorkflow(objects, access, grant, &workflow);
  if (!opened.ok()) return opened;
  const auto* handlers = workflow.Find("chatkit_actions");
  const auto* code =
      handlers ? handlers->Find(action->GetString("type")) : nullptr;
  if (!code)
    return Error(404, "chatkit_action_not_found",
                 "Workflow action not registered");
  const auto* state = data.Find("action_state");
  const auto* payload = action->Find("payload");
  const auto* variables =
      grant.data.Find("session")->Find("workflow")->Find("state_variables");
  const auto now = std::time(nullptr);
  auto context =
      Object({{"action", Object({{"type", *action->Find("type")},
                                 {"payload", payload ? *payload : Json{}}})},
              {"thread", Field(data, "thread")},
              {"sender", sender ? ClientItem(*sender) : Json{}},
              {"state", state ? *state : Json{}},
              {"state_variables", variables ? *variables : Object()},
              {"streaming", Boolean(streaming)}});
  auto run = Execute(access, *code, std::move(context), now, progress);
  if (!run.ok()) return run;
  Json events = Array(), visible;
  auto prepared = PrepareEvents(run.value, streaming, &events);
  if (!prepared.ok()) return prepared;
  const auto* widget = run.value.Find("widget");
  const bool update = widget && !widget->IsNull();
  const auto* remove = run.value.Find("remove_sender");
  if (remove && (!remove->IsBool() ||
                 (remove->boolean && (!streaming || !sender || update))))
    return BadOutput();
  const bool erase = remove && remove->boolean;
  const auto* title = run.value.Find("title");
  if (title && !title->IsString() && !title->IsNull()) return BadOutput();
  if (update) {
    const auto type = widget->GetString("type");
    if ((type != "Card" && type != "Basic" && type != "ListView") ||
        !WidgetTree(*widget))
      return BadOutput();
    auto item =
        sender ? *sender
               : Object({{"id",
                          String(NewObjectId(ObjectKind::kConversationItem))},
                         {"thread_id", String(id)},
                         {"created_at", Number(now)},
                         {"object", String("chatkit.thread_item")},
                         {"type", String("chatkit.widget")}});
    if (item.GetString("id").empty())
      return Error(503, "chatkit_id_unavailable", "No widget ID");
    Set(&item, "widget", String(OutputJson(*widget)));
    visible = ClientItem(item);
    if (sender)
      *sender = std::move(item);
    else
      items.push_back(std::move(item));
    if (streaming) {
      if (!sender)
        events.array.push_back(
            Object({{"type", String("thread.item.added")}, {"item", visible}}));
      events.array.push_back(Object(
          {{"type",
            String(sender ? "thread.item.replaced" : "thread.item.done")},
           {"item", visible}}));
    }
  }
  if (erase) {
    const auto sender_key = *sender_id;
    std::erase_if(items, [&](const Json& item) {
      return item.GetString("id") == sender_key.str;
    });
    events.array.push_back(Object(
        {{"type", String("thread.item.removed")}, {"item_id", sender_key}}));
  }
  if (title) {
    Set(&Field(data, "thread"), "title", *title);
    if (streaming)
      events.array.push_back(Object({{"type", String("thread.updated")},
                                     {"thread", ClientThread(data)}}));
  }
  const auto* next_state = run.value.Find("state");
  if (next_state) Set(&data, "action_state", *next_state);
  if (next_state || update || erase || title) {
    ObjectRequest put;
    put.kind = ObjectKind::kChatKitThread;
    put.operation = ObjectOperation::kPut;
    put.scope = access.scope;
    put.id = id;
    put.data = std::move(data);
    put.expected_revision = row.value.GetInt("revision");
    put.expires_at = row.value.GetInt("expires_at");
    auto saved = objects.Execute(put, access.deadline, access.cancelled);
    if (!saved.ok()) return saved;
  }
  ObjectReply reply;
  reply.value = streaming ? std::move(events)
                          : Object({{"updated_item", std::move(visible)}});
  return reply;
}
}  // namespace q4t::server::chatkit_detail
