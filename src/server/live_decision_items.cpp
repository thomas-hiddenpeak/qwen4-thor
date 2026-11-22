#include "q4t/server/live_decision_items.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/decision_plan.h"
#include "q4t/server/http_response.h"
#include "q4t/server/realtime_conversation.h"

namespace q4t::server {
using namespace object_detail;
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
    ChatPlanError error;
    size_t breakpoints = 0;
    const auto checked =
        CheckDecisionInputItem(*input, "item", &breakpoints, &error);
    if (!checked) {
      auto failure = Error(error.http_status, error.code, checked.message());
      failure.value = Object({{"param", String(error.param)}});
      return failure;
    }
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
