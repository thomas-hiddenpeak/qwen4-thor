#include "q4t/server/live_decision_items.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/completion_response.h"
#include "q4t/server/http_response.h"
#include "q4t/server/realtime_conversation.h"

namespace q4t::server {
using namespace object_detail;
namespace {
bool ToolIdentifier(std::string_view name, size_t limit, bool allow_dot) {
  return !name.empty() && name.size() <= limit &&
         std::ranges::all_of(name, [allow_dot](char c) {
           return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '_' || c == '-' ||
                  (allow_dot && c == '.');
         });
}
ObjectReply CheckItem(const Json& item) {
  auto invalid = [](std::string path) {
    auto reply = Error(400, "invalid_request", "Invalid Decision item field");
    reply.value = Object({{"param", String("item." + path)}});
    return reply;
  };
  if (const auto* call = item.Find("call_id");
      call && (call->str.empty() || Utf8Characters(call->str) > 64))
    return invalid("call_id");
  const bool function = item.GetString("type") == "function_call";
  if (const auto* name = item.Find("name");
      name && !ToolIdentifier(name->str, function ? 128 : 64, function))
    return invalid("name");
  if (const auto* space = item.Find("namespace");
      space && !space->IsNull() &&
      !ToolIdentifier(space->str, function ? 128 : 64, false))
    return invalid("namespace");
  for (const auto key : {"content", "output"}) {
    const auto* parts = item.GetArray(key);
    if (!parts) continue;
    for (size_t i = 0; i < parts->array.size(); ++i) {
      const auto* url = parts->array[i].Find("image_url");
      if (url && !url->str.starts_with("data:") &&
          !url->str.starts_with("https://") && !url->str.starts_with("http://"))
        return invalid(std::string(key) + "[" + std::to_string(i) +
                       "].image_url");
    }
  }
  return {};
}
}  // namespace

ObjectReply LiveDecisionItems::Apply(const Json& event,
                                     const LiveEventSink& emit) {
  constexpr size_t kItems = 128, kBytes = 8ULL << 20;
  const bool create = event.GetString("type") == "decision.item.create";
  if (!create && event.GetString("type") != "decision.item.delete")
    return Error(400, "invalid_request", "Expected a Decision item command");
  const auto* input = event.Find("item");
  if ((create && (!input || !input->IsObject())) ||
      (!create && !event.Has("item_id")))
    return Error(400, "invalid_request", "Decision item is missing");
  const auto* supplied = create ? input->Find("id") : event.Find("item_id");
  auto id = supplied && !supplied->IsNull() ? supplied->str
                                            : detail::NewRequestId("item_");
  const auto existing = std::ranges::find_if(items_, [&](const Entry& entry) {
    return entry.item.Find("id")->str == id;
  });
  auto reply = Object();
  if (create) {
    auto checked = CheckItem(*input);
    if (!checked.ok()) return checked;
    if (existing != items_.end())
      return Error(400, "duplicate_item_id", "Decision item ID already exists");
    auto item = *input;
    Set(&item, "id", String(id));
    size_t bytes = 0;
    if (items_.size() >= kItems ||
        !MeasureRealtimeStorage(item, kBytes - bytes_, &bytes))
      return Error(429, "decision_item_capacity", "Decision context is full");
    reply = Object({{"type", String("decision.item.created")}, {"item", item}});
    items_.push_back({std::move(item), bytes});
    bytes_ += bytes;
  } else {
    if (existing == items_.end())
      return Error(400, "item_not_found", "Decision item ID was not found");
    reply = Object(
        {{"type", String("decision.item.deleted")}, {"item_id", String(id)}});
    bytes_ -= existing->bytes;
    items_.erase(existing);
  }
  if (const auto* client = event.Find("event_id"); client && client->IsString())
    Set(&reply, "client_event_id", *client);
  auto result = emit(std::move(reply));
  if (!result.ok() || result.http_status < 200 || result.http_status >= 300)
    result.commit_unknown = true;
  return result;
}

Json LiveDecisionItems::Snapshot() const {
  auto items = Array();
  items.array.reserve(items_.size());
  for (const auto& entry : items_) items.array.push_back(entry.item);
  return items;
}
}  // namespace q4t::server
