#include "q4t/server/agent_session.h"

#include <unordered_map>

#include "agent_session_internal.h"
#include "object_protocol.h"
#include "q4t/server/conversation_state.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace agent_detail;

ObjectReply InvalidTrace() {
  return Error(503, "agent_session_record_invalid",
               "Invalid session trace history");
}

bool ValidTime(int64_t seconds) {
  // OTLP fixed64 nanoseconds. Persistent Agent timestamps have second
  // resolution.
  return seconds > 0 && seconds <= 18446744073LL;
}

Json Nano(int64_t seconds) {
  return String(std::to_string(seconds) + "000000000");
}

Json Attribute(std::string key, std::string value) {
  return Object(
      {{"key", String(std::move(key))},
       {"value", Object({{"stringValue", String(std::move(value))}})}});
}

Json Span(const std::string& trace, const std::string& id, std::string name,
          int64_t start, int64_t end, const std::string& status) {
  auto span =
      Object({{"traceId", String(trace)},
              {"spanId", String(id)},
              {"name", String(std::move(name))},
              {"kind", Number(1)},
              {"startTimeUnixNano", Nano(start)},
              {"endTimeUnixNano", Nano(end)},
              {"attributes", Array({Attribute("q4t.status", status)})}});
  if (status == "failed" || status == "incomplete")
    Set(&span, "status", Object({{"code", Number(2)}}));
  return span;
}

ObjectReply TurnTrace(ObjectStore& objects, const Json& session_data,
                      const std::string& session_id, const std::string& turn,
                      std::span<const Json> links, const StateAccess& access) {
  auto spans = Array();
  const auto trace = Digest(session_id + "/" + turn).substr(0, 32);
  const auto root = Digest(trace + "/turn").substr(0, 16);
  int64_t created = 0;
  ResponsesState state(objects);
  for (const auto& link : links) {
    const auto response_id = link.GetString("response_id");
    auto saved = state.ReadRecord(response_id, access);
    if (!saved.ok()) return saved;
    const auto& record = saved.value;
    const auto* response = record.Find("response");
    if (!response || record.GetString("agent_session_id") != session_id)
      return InvalidTrace();
    const auto begin = response->GetInt("created_at");
    const auto end = record.GetInt("agent_turn_finished_at");
    const auto started = record.GetInt("agent_turn_started_at");
    if (!ValidTime(begin) || (end && (!ValidTime(end) || end < begin)))
      return InvalidTrace();
    if (!created) created = begin;
    if (end) {
      auto step =
          Span(trace, Digest(trace + "/" + response_id).substr(0, 16),
               "q4t.agent.step", begin, end, response->GetString("status"));
      Set(&step, "parentSpanId", String(root));
      if (started >= begin && started <= end)
        Set(&step, "events",
            Array({Object({{"name", String("response.in_progress")},
                           {"timeUnixNano", Nano(started)}})}));
      spans.array.push_back(std::move(step));
    }
    if (&link == &links.back()) {
      // Tool waiting may span several native responses. The same projection
      // used by the turns endpoint decides when the root actually finished.
      auto settings = SessionSettings(session_data, &link);
      if (!settings.ok()) return settings;
      AgentSessionView view;
      auto projected = ProjectStep(settings.value, link, record, &view);
      if (!projected.ok()) return projected;
      const auto closed = view.turn.GetInt("completed_at");
      if (closed) {
        if (!ValidTime(closed) || closed < created) return InvalidTrace();
        spans.array.push_back(Span(trace, root, "q4t.agent.turn", created,
                                   closed, view.turn.GetString("status")));
      }
    }
  }
  auto resources = Array();
  if (!spans.array.empty()) {
    auto scope = Object({{"scope", Object({{"name", String("q4t.serve")}})},
                         {"spans", std::move(spans)}});
    auto resource =
        Object({{"attributes", Array({Attribute("service.name", "q4t")})}});
    resources.array.push_back(
        Object({{"resource", std::move(resource)},
                {"scopeSpans", Array({std::move(scope)})}}));
  }
  ObjectReply result;
  result.value =
      Object({{"id", String(turn)},
              {"session_id", String(session_id)},
              {"object", String("agent.session.trace")},
              {"created_at", Number(created)},
              {"otlp", Object({{"resourceSpans", std::move(resources)}})}});
  return result;
}
}  // namespace

ObjectReply ListAgentSessionTraces(ObjectStore& objects, const std::string& id,
                                   const ItemViewOptions& paging,
                                   const StateAccess& access) {
  using namespace object_detail;
  auto row = agent_detail::ReadRow(objects, id, access);
  if (!row.ok()) return row;
  const auto* data = row.value.Find("data");
  const auto* session = data ? data->Find("session") : nullptr;
  const auto* links = data ? data->GetArray("turns") : nullptr;
  if (!session || session->GetString("id") != id || !links ||
      (links->array.empty() && !data->GetBool("created_idle")) || links->array.size() >= kMaxTransactionObjects)
    return InvalidTrace();
  std::unordered_map<std::string, std::span<const Json>> groups;
  auto roots = Array();
  std::span<const Json> remaining = links->array;
  while (!remaining.empty()) {
    const auto turn = remaining.front().GetString("id");
    if (!turn.starts_with("turn_") || !ValidId(turn)) return InvalidTrace();
    size_t count = 1;
    while (count < remaining.size() && remaining[count].GetString("id") == turn)
      ++count;
    if (!groups.emplace(turn, remaining.first(count)).second)
      return InvalidTrace();
    roots.array.push_back(Object({{"id", String(turn)}}));
    remaining = remaining.subspan(count);
  }
  auto page = PageStoredItems(roots, paging, StoredItemView::kAgentSession);
  if (!page.ok()) return page;
  for (auto& item : Field(page.value, "data").array) {
    const auto turn = item.GetString("id");
    auto trace = TurnTrace(objects, *data, id, turn, groups.at(turn), access);
    if (!trace.ok()) return trace;
    item = std::move(trace.value);
  }
  return page;
}
}  // namespace q4t::server
