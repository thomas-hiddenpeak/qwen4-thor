#include "q4t/server/realtime_mcp_item.h"

#include "../src/server/object_protocol.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using Phase = RealtimeMcpCallProjection::Phase;
Json Call() {
  return Object({{"type", String("mcp_call")},
                 {"id", String("mcp_read")},
                 {"server_label", String("project")},
                 {"name", String("read")},
                 {"arguments", String("{\"path\":\"docs/STATUS.md\"}")}});
}
Json Native(std::string status = "completed") {
  auto call = Call();
  Set(&call, "status", String(status));
  return call;
}
Json Approval(std::string id = "mcpr_read") {
  auto item = Call();
  Set(&item, "type", String("mcp_approval_request"));
  Set(&item, "id", String(id));
  return item;
}
RealtimeMcpCallProjection Sentinel() {
  return {String("unchanged"), Phase::kFailed};
}
}  // namespace

Q4T_TEST(realtime_mcp_call_projection_preserves_exact_actual_result) {
  auto native = Native();
  const std::string result =
      "{\"content\":[{\"type\":\"text\",\"text\":\"rev396\"}],"
      "\"structuredContent\":{\"version\":396}}";
  Set(&native, "output", String(result));
  Set(&native, "approval_request_id", String("mcpr_read"));
  Set(&native, "error", Json());
  const auto original = OutputJson(native);
  RealtimeMcpCallProjection projection;
  ChatPlanError error;
  Q4T_CHECK(ProjectRealtimeMcpCall(native, &projection, &error));
  Q4T_CHECK(projection.phase == Phase::kCompleted &&
            projection.item.GetString("output") == result &&
            projection.item.GetString("approval_request_id") == "mcpr_read" &&
            !projection.item.Has("status") && OutputJson(native) == original);
  Q4T_CHECK(
      ValidateRealtimeShape(projection.item, RealtimeShape::kItem, &error));
  Set(&native, "output", String(""));
  Q4T_CHECK(ProjectRealtimeMcpCall(native, &projection, &error) &&
            projection.phase == Phase::kCompleted &&
            projection.item.Find("output")->IsString() &&
            projection.item.GetString("output").empty());
  return true;
}

Q4T_TEST(realtime_mcp_call_projection_preserves_all_native_error_families) {
  for (const auto kind : {"mcp_protocol_error", "http_error"}) {
    auto native = Native("failed");
    const auto failure = Object({{"type", String(kind)},
                                 {"code", Number(-32603)},
                                 {"message", String("actual failure")}});
    Set(&native, "error", failure);
    RealtimeMcpCallProjection projection;
    ChatPlanError error;
    Q4T_CHECK(ProjectRealtimeMcpCall(native, &projection, &error));
    const auto* mapped = projection.item.Find("error");
    Q4T_CHECK(projection.phase == Phase::kFailed && mapped &&
              mapped->GetString("type") ==
                  (std::string_view(kind) == "http_error" ? "http_error"
                                                          : "protocol_error") &&
              mapped->GetInt("code") == -32603 &&
              mapped->GetString("message") == "actual failure");
  }
  for (const auto& content :
       {String("remote denial"), Object({{"details", String("remote denial")}}),
        Array({String("one"), String("two")})}) {
    auto native = Native("failed");
    Set(&native, "error",
        Object({{"type", String("mcp_tool_execution_error")},
                {"content", content}}));
    RealtimeMcpCallProjection projection;
    ChatPlanError error;
    Q4T_CHECK(ProjectRealtimeMcpCall(native, &projection, &error));
    const auto* mapped = projection.item.Find("error");
    Q4T_CHECK(mapped->GetString("type") == "tool_execution_error" &&
              mapped->GetString("message") ==
                  (content.IsString() ? content.str : OutputJson(content)));
  }
  return true;
}

Q4T_TEST(realtime_mcp_call_projection_pending_never_invents_completion) {
  for (const auto state : {"in_progress", "calling"}) {
    auto native = Native(state);
    Set(&native, "output", Json());
    RealtimeMcpCallProjection projection;
    ChatPlanError error;
    Q4T_CHECK(ProjectRealtimeMcpCall(native, &projection, &error));
    Q4T_CHECK(projection.phase == Phase::kPending &&
              projection.item.Find("output")->IsNull() &&
              !projection.item.Has("status"));
  }
  return true;
}

Q4T_TEST(realtime_mcp_call_projection_rejects_contradictory_execution_state) {
  for (const auto state :
       {"completed", "failed", "incomplete", "unknown", ""}) {
    auto native = Native(state);
    auto projection = Sentinel();
    ChatPlanError error;
    Q4T_CHECK(!ProjectRealtimeMcpCall(native, &projection, &error));
    Q4T_CHECK(projection.item.str == "unchanged" &&
              projection.phase == Phase::kFailed);
  }
  for (const auto state : {"completed", "in_progress", "calling"}) {
    auto native = Native(state);
    Set(&native, "output", String("unproven result"));
    Set(&native, "error",
        Object({{"type", String("http_error")},
                {"code", Number(502)},
                {"message", String("transport failed")}}));
    auto projection = Sentinel();
    ChatPlanError error;
    Q4T_CHECK(!ProjectRealtimeMcpCall(native, &projection, &error));
    Q4T_CHECK(projection.item.str == "unchanged");
  }
  return true;
}

Q4T_TEST(realtime_mcp_call_projection_rejects_private_fields_and_bad_identity) {
  auto valid = Native();
  Set(&valid, "output", String("result"));
  for (const auto field : {"authorization", "headers", "session"}) {
    auto native = valid;
    Set(&native, field, String("fixture-only-private"));
    auto projection = Sentinel();
    ChatPlanError error;
    Q4T_CHECK(!ProjectRealtimeMcpCall(native, &projection, &error) &&
              projection.item.str == "unchanged");
  }
  for (const auto id : {"client_call", "mcp_ bad", ""}) {
    auto native = valid;
    Set(&native, "id", String(id));
    auto projection = Sentinel();
    ChatPlanError error;
    Q4T_CHECK(!ProjectRealtimeMcpCall(native, &projection, &error));
  }
  valid.object.push_back({"name", String("read")});
  auto projection = Sentinel();
  ChatPlanError error;
  Q4T_CHECK(!ProjectRealtimeMcpCall(valid, &projection, &error) &&
            projection.item.str == "unchanged");
  return true;
}

Q4T_TEST(realtime_mcp_call_projection_bounds_native_and_expanded_storage) {
  auto native = Native();
  Set(&native, "output", String(std::string(4ULL << 20, 'x')));
  auto projection = Sentinel();
  ChatPlanError error;
  Q4T_CHECK(!ProjectRealtimeMcpCall(native, &projection, &error) &&
            error.http_status == 413 && projection.item.str == "unchanged");
  native = Native("failed");
  Set(&native, "error",
      Object({{"type", String("mcp_tool_execution_error")},
              {"content",
               Object({{"escaped", String(std::string(800000, '\1'))}})}}));
  size_t bytes = 0;
  Q4T_CHECK(MeasureRealtimeStorage(native, 4ULL << 20, &bytes));
  Q4T_CHECK(!ProjectRealtimeMcpCall(native, &projection, &error) &&
            error.http_status == 413 && projection.item.str == "unchanged");
  return true;
}

Q4T_TEST(realtime_mcp_approval_binds_once_and_repeats_without_mutation) {
  RealtimeConversation history("sess_approval");
  RealtimeItemTicket ticket;
  auto call = Call();
  Set(&call, "approval_request_id", Json());
  Q4T_CHECK(history.BeginOutput(call, "unused", true, 8192, &ticket));
  Q4T_CHECK(history.BindMcpApproval(ticket, Approval()));
  const auto revision = history.Revision(), charged = history.ChargedBytes();
  Q4T_CHECK(history.BindMcpApproval(ticket, Approval()));
  Q4T_CHECK(history.Revision() == revision &&
            history.ChargedBytes() == charged);
  Json actual;
  Q4T_CHECK(history.Get(ticket, &actual) &&
            actual.GetString("approval_request_id") == "mcpr_read");
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(!history.Snapshot(nullptr, &snapshot));
  return true;
}

Q4T_TEST(realtime_mcp_approval_requires_exact_call_and_native_output_owner) {
  RealtimeConversation history("sess_approval");
  RealtimeItemTicket ticket, client, audio;
  Q4T_CHECK(history.BeginOutput(Call(), "unused", true, 8192, &ticket));
  const auto revision = history.Revision();
  for (const auto field : {"name", "server_label", "arguments", "id", "type"}) {
    auto approval = Approval();
    Set(&approval, field, String("different"));
    Q4T_CHECK(!history.BindMcpApproval(ticket, approval));
  }
  Q4T_CHECK(history.Revision() == revision);
  auto call = Call();
  Set(&call, "id", String("mcp_client"));
  Q4T_CHECK(history.Create(call, "unused", {}, &client));
  Q4T_CHECK(!history.BindMcpApproval(client, Approval()));
  Q4T_CHECK(history.BeginAudioInput("audio", {}, true, 8192, &audio));
  Q4T_CHECK(!history.BindMcpApproval(audio, Approval()));
  return true;
}

Q4T_TEST(realtime_mcp_approval_rejects_foreign_stale_and_completed_ticket) {
  RealtimeConversation history("sess_approval"), foreign("sess_foreign");
  RealtimeItemTicket ticket, outside, fresh;
  Q4T_CHECK(history.BeginOutput(Call(), "unused", true, 8192, &ticket));
  Q4T_CHECK(foreign.BeginOutput(Call(), "unused", true, 8192, &outside));
  Q4T_CHECK(!history.BindMcpApproval(outside, Approval()));
  Q4T_CHECK(history.Erase(ticket));
  Q4T_CHECK(history.BeginOutput(Call(), "unused", true, 8192, &fresh));
  Q4T_CHECK(!history.BindMcpApproval(ticket, Approval()));
  Q4T_CHECK(history.BindMcpApproval(fresh, Approval()));
  Json call;
  Q4T_CHECK(history.Get(fresh, &call));
  Set(&call, "output", String("actual result"));
  Q4T_CHECK(history.Update(fresh, call, true));
  Q4T_CHECK(!history.BindMcpApproval(fresh, Approval()));
  return true;
}

Q4T_TEST(realtime_mcp_approval_cannot_rebind_through_updates_or_snapshots) {
  RealtimeConversation history("sess_approval");
  RealtimeItemTicket ticket;
  Q4T_CHECK(history.BeginOutput(Call(), "unused", true, 8192, &ticket));
  auto changed = Call();
  Set(&changed, "approval_request_id", String("mcpr_other"));
  Q4T_CHECK(!history.Update(ticket, changed, false, true));
  Q4T_CHECK(history.BindMcpApproval(ticket, Approval()));
  Q4T_CHECK(!history.BindMcpApproval(ticket, Approval("mcpr_other")));
  Q4T_CHECK(!history.Update(ticket, changed, false, true));
  RealtimeConversationSnapshot captured;
  Q4T_CHECK(history.Capture(nullptr, &captured, true));
  Set(&captured.items.array[0], "approval_request_id", String("mcpr_other"));
  bool ready = true;
  Q4T_CHECK(!history.Resolve(&captured, &ready) && !ready);
  return true;
}

Q4T_TEST(realtime_mcp_approval_capacity_failure_leaves_history_unchanged) {
  const auto call = Call();
  size_t bytes = 0;
  Q4T_CHECK(MeasureRealtimeStorage(call, 16384, &bytes));
  RealtimeConversation history("sess_approval", 1, bytes);
  RealtimeItemTicket ticket;
  Q4T_CHECK(history.BeginOutput(call, "unused", true, bytes, &ticket));
  const auto revision = history.Revision(), charged = history.ChargedBytes();
  Q4T_CHECK(!history.BindMcpApproval(ticket, Approval()));
  Json actual;
  Q4T_CHECK(history.Get(ticket, &actual) &&
            OutputJson(actual) == OutputJson(call));
  Q4T_CHECK(history.Revision() == revision &&
            history.ChargedBytes() == charged);
  return true;
}

Q4T_TEST(realtime_mcp_approval_resolves_capture_only_after_actual_completion) {
  RealtimeConversation history("sess_approval");
  RealtimeItemTicket ticket;
  Q4T_CHECK(history.BeginOutput(Call(), "unused", true, 8192, &ticket));
  RealtimeConversationSnapshot captured;
  Q4T_CHECK(history.Capture(nullptr, &captured, true));
  const auto original = OutputJson(captured.items);
  Q4T_CHECK(history.BindMcpApproval(ticket, Approval()));
  bool ready = true;
  Q4T_CHECK(history.Resolve(&captured, &ready) && !ready &&
            OutputJson(captured.items) == original);
  Json actual;
  Q4T_CHECK(history.Get(ticket, &actual));
  Set(&actual, "output", String("actual remote result"));
  Q4T_CHECK(history.Update(ticket, actual, true));
  Q4T_CHECK(
      history.Resolve(&captured, &ready) && ready &&
      captured.pending_outputs.empty() &&
      captured.items.array[0].GetString("approval_request_id") == "mcpr_read" &&
      captured.items.array[0].GetString("output") == "actual remote result");
  return true;
}
