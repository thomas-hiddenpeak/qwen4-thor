#include "chatkit_thread_state.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"

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

ObjectReply Finish(const Json& response, Json* data) {
  auto& items = Field(*data, "items");
  const auto* output = response.GetArray("output");
  if (!items.IsArray() || !output) return Corrupt();
  const auto id = Field(*data, "thread").GetString("id");
  for (const auto& item : output->array) {
    if (item.GetString("type") != "message" ||
        item.GetString("role") != "assistant")
      continue;
    const auto* content = item.GetArray("content");
    if (!content) return Corrupt();
    Json parts = Array();
    for (const auto& part : content->array) {
      if (part.GetString("type") != "output_text") continue;
      parts.array.push_back(Object({{"type", String("output_text")},
                                    {"text", String(part.GetString("text"))},
                                    {"annotations", Array()}}));
    }
    if (parts.array.empty()) continue;
    items.array.push_back(Object({
        {"id", String(item.GetString("id"))}, {"thread_id", String(id)},
        {"object", String("chatkit.thread_item")},
        {"type", String("chatkit.assistant_message")},
        {"created_at", Number(response.GetInt("created_at"))},
        {"content", std::move(parts)}}));
  }
  Set(data, "last_response", String(response.GetString("id")));
  Set(data, "last_status", String(response.GetString("status")));
  Set(data, "pending_response", {});
  return {};
}
}  // namespace

ObjectReply ReadThread(ObjectStore& objects, const StateAccess& access,
                       const std::string& id, std::string_view user) {
  ObjectRequest request;
  request.scope = access.scope;
  request.kind = ObjectKind::kChatKitThread;
  request.id = id;
  for (int attempt = 0; attempt != 4; ++attempt) {
    request.operation = ObjectOperation::kGet;
    auto found = objects.Execute(request, access.deadline, access.cancelled);
    if (!found.ok()) return found;
    auto& data = Field(found.value, "data");
    const auto* thread = data.Find("thread");
    ChatPlanError error;
    if (!thread || !ValidateChatKit(*thread, "thread", &error) ||
        thread->GetString("id") != id || !data.GetArray("items"))
      return Corrupt();
    if (!user.empty() && thread->GetString("user") != user)
      return Error(404, "object_not_found", "Thread not found");
    const auto pending = data.GetString("pending_response");
    if (pending.empty()) return found;
    ResponsesState state(objects);
    auto response = state.Retrieve(pending, access);
    if (!response.ok()) return response;
    const auto status = response.value.GetString("status");
    if (status == "queued" || status == "in_progress") return found;
    if (status != "completed" && status != "incomplete" && status != "failed" &&
        status != "cancelled") return Corrupt();
    auto prepared = Finish(response.value, &data);
    if (!prepared.ok()) return prepared;
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
  std::erase_if(item.object, [](const auto& field) { return field.first == "object"; });
  auto type = item.GetString("type");
  if (type.starts_with("chatkit.")) type.erase(0, 8);
  Set(&item, "type", String(type));
  Set(&item, "created_at", String(Date(item.GetInt("created_at"))));
  if (type == "user_message") {
    auto& options = Field(item, "inference_options");
    if (options.IsNull()) options = Object();
  }
  return item;
}

io::Json ClientThread(const io::Json& data) {
  const auto& t = *data.Find("thread");
  auto items = Array();
  for (const auto& item : data.GetArray("items")->array)
    items.array.push_back(ClientItem(item));
  return Object({{"id", String(t.GetString("id"))},
                 {"created_at", String(Date(t.GetInt("created_at")))},
                 {"title", *t.Find("title")}, {"status", *t.Find("status")},
                 {"items", Object({{"data", std::move(items)},
                                   {"has_more", Boolean(false)}, {"after", {}}})}});
}
}  // namespace q4t::server::chatkit_detail
