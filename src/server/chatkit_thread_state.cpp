#include "chatkit_thread_state.h"

#include "chatkit_structured.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/request_json.h"
#include "chatkit_credentials.h"

namespace q4t::server::chatkit_detail {
namespace {
using namespace object_detail;
std::string Date(int64_t timestamp) {
  const std::time_t time = timestamp;
  std::tm utc{};
  char result[32]{};
  if (!gmtime_r(&time, &utc) ||
      !std::strftime(result, sizeof(result), "%Y-%m-%dT%H:%M:%SZ", &utc))
    return {};
  return result;
}

ObjectReply Corrupt() {
  return Error(503, "chatkit_thread_invalid", "Invalid stored ChatKit thread");
}

bool Owns(const Json& data, const Grant& grant) {
  const auto& session = *grant.data.Find("session");
  const auto* thread = data.Find("thread");
  const bool own = data.GetString("created_by") == grant.session;
  const auto& history = *session.Find("chatkit_configuration")->Find("history");
  return thread && thread->GetString("user") == session.GetString("user") &&
         data.GetString("workflow") ==
             session.Find("workflow")->GetString("id") &&
         (own || (history.GetBool("enabled") && !data.GetBool("session_only")));
}
ObjectReply Missing() {
  return Error(404, "object_not_found", "Thread not found");
}
}  // namespace

ObjectReply AppendUserContent(const Json& parts, Json* content, Json* model) {
  const bool tagged = std::ranges::any_of(parts.array, [](const Json& part) {
    return part.GetString("type") == "input_tag";
  });
  std::string rendered;
  for (const auto& part : parts.array) {
    const auto type = part.GetString("type");
    const auto* text = part.Find("text");
    if (!text || !text->IsString() ||
        (type != "input_text" && type != "input_tag"))
      return Error(400, "invalid_request", "Expected a ChatKit text or tag");
    auto wire = Object({{"type", String(type)}, {"text", *text}});
    if (type == "input_tag") {
      const auto* id = part.Find("id");
      const auto* data = part.Find("data");
      const auto* group = part.Find("group");
      const auto* interactive = part.Find("interactive");
      if (!id || !id->IsString() || !data || !data->IsObject() ||
          (group && !group->IsNull() && !group->IsString()) ||
          (interactive && !interactive->IsBool()))
        return Error(400, "invalid_request", "Invalid ChatKit entity tag");
      Set(&wire, "id", *id);
      Set(&wire, "data", *data);
      Set(&wire, "group", group ? *group : Json{});
      Set(&wire, "interactive", interactive ? *interactive : Boolean(false));
      rendered += '@';
    }
    if (tagged)
      rendered += text->str;
    else
      model->array.push_back(wire);
    content->array.push_back(std::move(wire));
  }
  if (tagged)
    model->array.push_back(Object({{"type", String("input_text")},
                                   {"text", String(std::move(rendered))}}));
  return {};
}

void ProjectManagementItem(Json* item) {
  // Management exposes the original typed function call and its result.
  std::erase_if(item->object, [](const auto& field) {
    return field.first == "browser_form";
  });
  if (item->GetString("type") != "chatkit.user_message") return;
  for (auto& part : Field(*item, "content").array) {
    const auto* text = part.Find("text");
    if (part.GetString("type") == "input_tag" && text && text->IsString())
      part = Object(
          {{"type", String("input_text")}, {"text", String("@" + text->str)}});
  }
}

ObjectReply ProjectOutput(const Json& response, const std::string& id,
                          std::string_view form_tool) {
  const auto* output = response.GetArray("output");
  if (!output) return Corrupt();
  auto items = Array();
  for (const auto& item : output->array) {
    if (item.GetString("type") == "function_call") {
      Json arguments;
      if (item.GetString("call_id").empty() || item.GetString("name").empty() ||
          !io::ParseJson(item.GetString("arguments"), &arguments, true,
                         kApplicationJsonLimits) ||
          !arguments.IsObject())
        return Corrupt();
      items.array.push_back(
          Object({{"id", String(item.GetString("id"))},
                  {"thread_id", String(id)},
                  {"created_at", Number(response.GetInt("created_at"))},
                  {"object", String("chatkit.thread_item")},
                  {"type", String("chatkit.client_tool_call")},
                  {"status", String("in_progress")},
                  {"name", String(item.GetString("name"))},
                  {"arguments", String(item.GetString("arguments"))},
                  {"call_id", String(item.GetString("call_id"))},
                  {"output", {}}}));
      if (!form_tool.empty() && item.GetString("name") == form_tool) {
        auto form = PendingStructuredInput(arguments);
        if (!form.IsNull())
          Set(&items.array.back(), "browser_form", std::move(form));
      }
      continue;
    }
    if (item.GetString("type") != "message" ||
        item.GetString("role") != "assistant")
      continue;
    const auto* content = item.GetArray("content");
    if (!content) return Corrupt();
    Json parts = Array();
    for (const auto& part : content->array) {
      const bool refusal = part.GetString("type") == "refusal";
      if (!refusal && part.GetString("type") != "output_text") continue;
      parts.array.push_back(Object(
          {{"type", String("output_text")},
           {"text", String(part.GetString(refusal ? "refusal" : "text"))},
           {"annotations", Array()}}));
    }
    if (parts.array.empty()) continue;
    items.array.push_back(
        Object({{"id", String(item.GetString("id"))},
                {"thread_id", String(id)},
                {"object", String("chatkit.thread_item")},
                {"type", String("chatkit.assistant_message")},
                {"created_at", Number(response.GetInt("created_at"))},
                {"content", std::move(parts)}}));
  }
  ObjectReply result;
  result.value = std::move(items);
  return result;
}

ObjectReply BrowserHistory(ObjectStore& objects, const StateAccess& access,
                           const Grant& grant) {
  const auto& session = *grant.data.Find("session");
  const auto& history = *session.Find("chatkit_configuration")->Find("history");
  const auto limit = history.GetInt("recent_threads", -1);
  int64_t prior = 0;
  ObjectRequest scan;
  scan.operation = ObjectOperation::kList;
  scan.kind = ObjectKind::kChatKitThread;
  scan.scope = access.scope;
  scan.limit = 100;
  scan.descending = true;
  auto rows = Array();
  for (;;) {
    auto page = objects.Execute(scan, access.deadline, access.cancelled);
    if (!page.ok()) return page;
    const auto* records = page.value.GetArray("data");
    if (!records) return Corrupt();
    for (const auto& record : records->array) {
      const auto* data = record.Find("data");
      ChatPlanError error;
      if (!data || !data->Find("thread") ||
          !ValidateChatKit(*data->Find("thread"), "thread", &error) ||
          data->Find("thread")->GetString("id") != record.GetString("id"))
        return Corrupt();
      if (!Owns(*data, grant)) continue;
      if (data->GetString("created_by") != grant.session && limit >= 0 &&
          prior++ >= limit)
        continue;
      rows.array.push_back(*data->Find("thread"));
    }
    if (!page.value.GetBool("has_more")) break;
    const auto next = page.value.GetString("last_id");
    if (next.empty() || next == scan.after) return Corrupt();
    scan.after = next;
  }
  // PageAssistantRows consumes creation order, reversing it for desc.
  std::ranges::reverse(rows.array);
  ObjectReply result;
  result.value = std::move(rows);
  return result;
}

ObjectReply ReadThread(ObjectStore& objects, const StateAccess& access,
                       const std::string& id, const Grant* grant) {
  if (!id.starts_with("cthr_") || !ValidApplicationId(id)) return Missing();
  ObjectRequest read;
  read.scope = access.scope;
  read.kind = ObjectKind::kChatKitThread;
  read.id = id;
  for (int attempt = 0; attempt != 4; ++attempt) {
    auto found = objects.Execute(read, access.deadline, access.cancelled);
    if (!found.ok()) return found;
    auto& data = Field(found.value, "data");
    const auto* thread = data.Find("thread");
    ChatPlanError error;
    if (!thread || !ValidateChatKit(*thread, "thread", &error) ||
        thread->GetString("id") != id || !data.GetArray("items"))
      return Corrupt();
    if (grant) {
      if (!Owns(data, *grant)) return Missing();
      const auto& history = *grant->data.Find("session")
                                 ->Find("chatkit_configuration")
                                 ->Find("history");
      if (data.GetString("created_by") != grant->session &&
          history.GetInt("recent_threads", -1) >= 0) {
        auto visible = BrowserHistory(objects, access, *grant);
        if (!visible.ok()) return visible;
        if (std::ranges::none_of(visible.value.array, [&](const Json& t) {
              return t.GetString("id") == id;
            }))
          return Missing();
      }
    }
    const auto pending = data.GetString("pending_response");
    if (pending.empty()) return found;
    ResponsesState state(objects);
    auto response = state.Retrieve(pending, access);
    if (!response.ok()) return response;
    const auto status = response.value.GetString("status");
    if (status == "queued" || status == "in_progress") return found;
    if (status != "completed" && status != "incomplete" && status != "failed" &&
        status != "cancelled")
      return Corrupt();
    auto projected =
        ProjectOutput(response.value, id, data.GetString("pending_form_tool"));
    if (!projected.ok()) return projected;
    auto& items = Field(data, "items").array;
    for (auto& item : projected.value.array) items.push_back(std::move(item));
    Set(&data, "last_response", String(pending));
    Set(&data, "pending_response", {});
    std::erase_if(data.object, [](const auto& field) {
      return field.first == "pending_form_tool";
    });
    auto request = read;
    request.operation = ObjectOperation::kPut;
    request.expected_revision = found.value.GetInt("revision");
    request.expires_at = found.value.GetInt("expires_at");
    request.data = data;
    auto result = objects.Execute(request, access.deadline, access.cancelled);
    if (result.ok()) continue;
    if (result.code != "object_revision_conflict") return result;
  }
  return Error(409, "chatkit_thread_busy", "Thread changed during retrieval");
}

io::Json ClientItem(io::Json item) {
  std::erase_if(item.object,
                [](const auto& field) { return field.first == "object"; });
  auto type = item.GetString("type");
  if (type.starts_with("chatkit.")) type.erase(0, 8);
  Set(&item, "type", String(type));
  Set(&item, "created_at", String(Date(item.GetInt("created_at"))));
  if (const auto* form = item.Find("browser_form");
      form && form->Find("status") && form->GetArray("inputs")) {
    return Object({{"type", String("structured_input")},
                   {"id", String(item.GetString("id"))},
                   {"thread_id", String(item.GetString("thread_id"))},
                   {"created_at", String(item.GetString("created_at"))},
                   {"status", *form->Find("status")},
                   {"inputs", *form->Find("inputs")}});
  }
  if (type == "widget") {
    Json widget;
    if (io::ParseJson(item.GetString("widget"), &widget, true,
                      kApplicationJsonLimits))
      Set(&item, "widget", std::move(widget));
  }
  if (type == "client_tool_call") {
    Json arguments, output;
    io::ParseJson(item.GetString("arguments"), &arguments, true,
                  kApplicationJsonLimits);
    if (item.GetString("status") == "in_progress")
      Set(&item, "status", String("pending"));
    if (!item.GetString("output").empty())
      io::ParseJson(item.GetString("output"), &output, true,
                    kApplicationJsonLimits);
    Set(&item, "arguments", std::move(arguments));
    Set(&item, "output", std::move(output));
  }
  if (type == "user_message") {
    auto& parts = Field(item, "content").array;
    Json quote;
    std::erase_if(parts, [&](const Json& part) {
      if (part.GetString("type") != "quoted_text") return false;
      quote = String(part.GetString("text"));
      return true;
    });
    if (!quote.IsNull()) Set(&item, "quoted_text", std::move(quote));
    auto& options = Field(item, "inference_options");
    if (options.IsNull()) options = Object();
  }
  return item;
}

io::Json ClientThread(const io::Json& data) {
  const auto& t = *data.Find("thread");
  auto items = Array();
  const auto* stored = data.GetArray("items");
  if (stored) {
    for (const auto& item : stored->array) {
      if (items.array.size() == 20) break;
      items.array.push_back(ClientItem(item));
    }
  }
  const bool more = stored && items.array.size() < stored->array.size();
  auto after = more ? String(items.array.back().GetString("id")) : Json{};
  return Object({{"id", String(t.GetString("id"))},
                 {"created_at", String(Date(t.GetInt("created_at")))},
                 {"title", *t.Find("title")},
                 {"status", *t.Find("status")},
                 {"items", Object({{"data", std::move(items)},
                                   {"has_more", Boolean(more)},
                                   {"after", std::move(after)}})}});
}
}  // namespace q4t::server::chatkit_detail
