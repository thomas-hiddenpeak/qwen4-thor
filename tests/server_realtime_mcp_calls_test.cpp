#include "q4t/server/realtime_mcp_calls.h"

#include <algorithm>
#include <array>
#include <stdexcept>
#include <atomic>
#include <map>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status < 300;
}
bool Wait(const std::function<bool()>& ready) {
  const auto deadline = Clock::now() + 2s;
  while (!ready()) {
    if (Clock::now() >= deadline) return false;
    std::this_thread::sleep_for(1ms);
  }
  return true;
}
Json Generated(std::string id) {
  return Object({{"type", String("mcp_call")},
                 {"id", String(id)},
                 {"name", String("read")},
                 {"server_label", String("project")},
                 {"arguments", String("{\"path\":\"docs/STATUS.md\"}")}});
}
Json Native(const Json& generated, bool success = true, bool pending = false) {
  auto item = generated;
  if (!item.Has("approval_request_id"))
    Set(&item, "approval_request_id", Json());
  Set(&item, "status",
      String(pending   ? "in_progress"
             : success ? "completed"
                       : "failed"));
  Set(&item, "output",
      success && !pending ? String("actual fixture result") : Json());
  Set(&item, "error",
      !success && !pending ? Object({{"type", String("mcp_protocol_error")},
                                     {"code", Number(-32603)},
                                     {"message", String("fixture cancelled")}})
                           : Json());
  return item;
}
struct Fixture {
  RealtimeConversation history{"sess_calls"};
  std::vector<Json> events;
  std::array<std::atomic<size_t>, 4> actions{};
  std::atomic<int> block{-1};
  std::atomic<bool> late_success{false}, observed_cancel{false},
      wrong_thread{false};
  std::map<std::string, Json> proposed, terminal;
  bool approval = false, maintenance_failed = false, fail_cleanup = false;
  bool ignore_deadline_while_blocked = false;
  size_t fail_event = SIZE_MAX, next_id = 0;
  std::function<void(RealtimeMcpAction, ObjectReply*)> change;
  const std::thread::id owner = std::this_thread::get_id();
  RealtimeMcpCatalog Catalog() {
    RealtimeMcpCatalog catalog;
    ChatPlanError error;
    if (!PlanResponsesMcpDefinition(
            Object(
                {{"type", String("mcp")},
                 {"server_label", String("project")},
                 {"server_url", String("https://example.com/mcp")},
                 {"require_approval", String(approval ? "always" : "never")}}),
            &catalog.definition, &error))
      throw std::runtime_error("fixture catalog");
    catalog.authority = Object(
        {{"type", String("mcp_list_tools")},
         {"id", String("mcpl_project")},
         {"server_label", String("project")},
         {"tools", Array({Object({{"name", String("read")},
                                  {"input_schema",
                                   Object({{"type", String("object")}})}})})}});
    catalog.item = catalog.authority;
    catalog.loaded = true;
    return catalog;
  }
  RealtimeMcpCallCallbacks Callbacks() {
    RealtimeMcpCallCallbacks callbacks;
    callbacks.new_id = [this](std::string_view prefix) {
      return std::string(prefix) + std::to_string(++next_id);
    };
    callbacks.emit = [this](const Json& event) {
      if (events.size() == fail_event)
        return Error(503, "sink_failed", "Fixture sink failed");
      events.push_back(event);
      return ObjectReply{};
    };
    callbacks.run = [this](const RealtimeMcpCallJob& job,
                           Clock::time_point deadline,
                           const std::function<bool()>& cancelled,
                           RealtimeMcpExecution* execution) {
      if (std::this_thread::get_id() == owner) wrong_thread = true;
      ++actions[static_cast<size_t>(job.action)];
      while (block == static_cast<int>(job.action) && !cancelled() &&
             (ignore_deadline_while_blocked || Clock::now() < deadline))
        std::this_thread::sleep_for(1ms);
      if (cancelled()) {
        observed_cancel = true;
        if (!late_success)
          return Error(499, "request_cancelled", "Fixture cancelled");
      }
      if (Clock::now() >= deadline)
        return Error(408, "request_timeout", "Fixture deadline");
      const auto id = job.generated.GetString("id");
      ObjectReply reply;
      if (job.action == RealtimeMcpAction::kPropose) {
        reply.value = Native(job.generated, true, true);
        proposed[id] = job.generated;
        if (approval) {
          reply.value = job.generated;
          Set(&reply.value, "type", String("mcp_approval_request"));
          Set(&reply.value, "id", String("mcpr_" + id));
          Set(&proposed[id], "approval_request_id", *reply.value.Find("id"));
        }
      } else if (job.action == RealtimeMcpAction::kDecide) {
        const auto approve = job.decision.GetBool("approve");
        reply.value = Native(job.generated, approve, approve);
        if (!approve) terminal[id] = reply.value;
      } else if (job.action == RealtimeMcpAction::kExecute) {
        reply.value = Native(job.generated);
        terminal[id] = reply.value;
        execution->item = reply.value;
        execution->call_permitted = true;
        execution->session_saved = !maintenance_failed;
        if (maintenance_failed)
          execution->session_error = "mcp_session_revision_conflict";
      } else {
        if (fail_cleanup)
          return Error(503, "object_unavailable",
                       "Fixture cleanup unavailable");
        reply.value = terminal.contains(id)
                          ? terminal.at(id)
                          : Native(proposed.contains(id) ? proposed.at(id)
                                                         : job.generated,
                                   false);
      }
      if (change) change(job.action, &reply);
      return reply;
    };
    return callbacks;
  }
  bool Add(RealtimeMcpCalls& calls, std::string id, RealtimeItemTicket* ticket,
           std::string response = "resp_owned", bool in_conversation = true) {
    const auto item = Generated(id);
    return history.BeginOutput(item, id, in_conversation, 8192, ticket) &&
           Accepted(calls.Begin(*ticket, {item, 0, 0}, Catalog(), response,
                                in_conversation));
  }
  bool Drain(RealtimeMcpCalls& calls) {
    bool okay = true;
    return Wait([&] {
             okay = Accepted(calls.Tick());
             return !okay || (!calls.Pending() && !calls.Workers());
           }) &&
           okay;
  }
  bool Approval(RealtimeMcpCalls& calls) {
    return Wait([&] {
             if (!Accepted(calls.Tick())) return true;
             return std::ranges::any_of(events, [](const auto& event) {
               const auto* item = event.Find("item");
               return event.GetString("type") == "conversation.item.done" &&
                      item && item->GetString("type") == "mcp_approval_request";
             });
           }) &&
           actions[2] == 0;
  }
  Json Decision(std::string id, bool approve) {
    return Object({{"type", String("mcp_approval_response")},
                   {"id", String("decision_" + id)},
                   {"approval_request_id", String("mcpr_" + id)},
                   {"approve", Boolean(approve)}});
  }
  bool Shapes() const {
    ChatPlanError error;
    for (const auto& event : events)
      if (!ValidateRealtimeShape(event, RealtimeShape::kServerEvent, &error))
        return false;
    return true;
  }
};
}  // namespace

Q4T_TEST(realtime_mcp_calls_generation_remains_pending_until_actual_result) {
  Fixture f;
  RealtimeMcpCalls calls(f.history, f.Callbacks());
  RealtimeItemTicket ticket;
  Q4T_CHECK(f.Add(calls, "mcp_a", &ticket));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(!f.history.Snapshot(nullptr, &snapshot) && f.actions[0] == 0);
  Q4T_CHECK(f.Drain(calls) && f.actions[0] == 1 && f.actions[2] == 1 &&
            !f.wrong_thread && f.Shapes());
  Q4T_CHECK(f.history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array[0].GetString("output") ==
                "actual fixture result");
  Q4T_CHECK(f.events.size() == 3 &&
            f.events[0].GetString("type") == "response.mcp_call.in_progress" &&
            f.events[1].GetString("type") == "response.output_item.done");
  return true;
}

Q4T_TEST(realtime_mcp_calls_approval_waits_for_exact_single_decision) {
  Fixture f;
  f.approval = true;
  RealtimeMcpCalls calls(f.history, f.Callbacks());
  RealtimeItemTicket ticket;
  Q4T_CHECK(f.Add(calls, "mcp_a", &ticket) && f.Approval(calls));
  Q4T_CHECK(!Accepted(calls.Decide(f.Decision("wrong", true))));
  const auto decision = f.Decision("mcp_a", true);
  Q4T_CHECK(Accepted(calls.Decide(decision)) &&
            !Accepted(calls.Decide(decision)));
  Q4T_CHECK(f.Drain(calls) && f.actions[1] == 1 && f.actions[2] == 1 &&
            f.Shapes());
  Json item;
  Q4T_CHECK(f.history.Get(ticket, &item) &&
            item.GetString("approval_request_id") == "mcpr_mcp_a");
  return true;
}

Q4T_TEST(realtime_mcp_calls_denial_finishes_without_dispatch) {
  Fixture f;
  f.approval = true;
  RealtimeMcpCalls calls(f.history, f.Callbacks());
  RealtimeItemTicket ticket;
  Q4T_CHECK(f.Add(calls, "mcp_a", &ticket) && f.Approval(calls));
  Q4T_CHECK(Accepted(calls.Decide(f.Decision("mcp_a", false))) &&
            f.Drain(calls));
  Json item;
  Q4T_CHECK(f.history.Get(ticket, &item) && !item.Find("error")->IsNull() &&
            f.actions[2] == 0 && f.Shapes());
  return true;
}

Q4T_TEST(realtime_mcp_calls_reject_client_stale_and_duplicate_handoffs) {
  Fixture f;
  RealtimeMcpCalls calls(f.history, f.Callbacks());
  RealtimeItemTicket client, ticket;
  auto item = Generated("mcp_client");
  Q4T_CHECK(f.history.Create(item, "unused", {}, &client));
  Q4T_CHECK(!Accepted(
      calls.Begin(client, {item, 0, 0}, f.Catalog(), "resp_owned", true)));
  Q4T_CHECK(f.Add(calls, "mcp_a", &ticket));
  Q4T_CHECK(!Accepted(calls.Begin(ticket, {Generated("mcp_a"), 0, 0},
                                  f.Catalog(), "resp_owned", true)));
  Q4T_CHECK(Accepted(calls.Erase(ticket)) && f.Drain(calls));
  Q4T_CHECK(!Accepted(calls.Begin(ticket, {Generated("mcp_a"), 0, 0},
                                  f.Catalog(), "resp_owned", true)));
  Q4T_CHECK(f.actions[2] == 0);
  return true;
}

Q4T_TEST(realtime_mcp_calls_deleted_active_generation_never_resurrects) {
  Fixture f;
  f.block = static_cast<int>(RealtimeMcpAction::kExecute);
  f.late_success = true;
  RealtimeMcpCalls calls(f.history, f.Callbacks());
  RealtimeItemTicket old, replacement;
  Q4T_CHECK(f.Add(calls, "mcp_a", &old));
  Q4T_CHECK(Wait([&] { return Accepted(calls.Tick()) && f.actions[2] == 1; }));
  Q4T_CHECK(Accepted(calls.Erase(old)));
  Q4T_CHECK(f.history.Create(Generated("mcp_a"), "unused", {}, &replacement));
  Q4T_CHECK(f.Drain(calls) && f.observed_cancel && f.actions[3] == 1);
  Json item;
  Q4T_CHECK(f.history.Get(replacement, &item) && !item.Has("output") &&
            f.Shapes());
  return true;
}

Q4T_TEST(realtime_mcp_calls_cancel_queued_work_never_proposes_or_executes) {
  Fixture f;
  RealtimeMcpCalls calls(f.history, f.Callbacks());
  RealtimeItemTicket ticket;
  Q4T_CHECK(f.Add(calls, "mcp_a", &ticket) &&
            calls.CancelResponse("resp_owned"));
  Q4T_CHECK(f.Drain(calls) && f.actions[0] == 0 && f.actions[2] == 0 &&
            f.Shapes());
  Q4T_CHECK(!calls.CancelResponse("resp_owned"));
  return true;
}

Q4T_TEST(realtime_mcp_calls_cancel_active_keeps_late_durable_completion) {
  Fixture f;
  f.block = static_cast<int>(RealtimeMcpAction::kExecute);
  f.late_success = true;
  RealtimeMcpCalls calls(f.history, f.Callbacks());
  RealtimeItemTicket ticket;
  Q4T_CHECK(f.Add(calls, "mcp_a", &ticket));
  Q4T_CHECK(Wait([&] { return Accepted(calls.Tick()) && f.actions[2] == 1; }));
  Q4T_CHECK(calls.CancelResponse("resp_owned") && f.Drain(calls));
  Json item;
  Q4T_CHECK(f.history.Get(ticket, &item) &&
            item.GetString("output") == "actual fixture result" &&
            f.actions[2] == 1 && f.actions[3] == 1);
  return true;
}

Q4T_TEST(realtime_mcp_calls_cancel_unpublished_approval_retains_native_denial) {
  Fixture f;
  f.approval = true;
  f.block = static_cast<int>(RealtimeMcpAction::kPropose);
  f.late_success = true;
  RealtimeMcpCalls calls(f.history, f.Callbacks());
  RealtimeItemTicket ticket;
  Q4T_CHECK(f.Add(calls, "mcp_a", &ticket));
  Q4T_CHECK(Accepted(calls.Tick()) && Wait([&] { return f.actions[0] == 1; }));
  Q4T_CHECK(calls.CancelResponse("resp_owned") && f.Drain(calls));
  Json item;
  Q4T_CHECK(f.history.Get(ticket, &item) &&
            item.GetString("approval_request_id") == "mcpr_mcp_a" &&
            item.Find("error")->GetString("message") == "fixture cancelled" &&
            f.actions[1] == 0 && f.actions[2] == 0 && f.actions[3] == 1 &&
            f.Shapes());
  Q4T_CHECK(std::ranges::none_of(f.events, [](const auto& event) {
    const auto* item = event.Find("item");
    return item && item->GetString("type") == "mcp_approval_request";
  }));
  return true;
}

Q4T_TEST(realtime_mcp_calls_capacity_failure_leaves_pending_owner_unchanged) {
  Fixture f;
  RealtimeMcpCallLimits limits;
  limits.retained_bytes = 1024;
  RealtimeMcpCalls calls(f.history, f.Callbacks(), limits);
  RealtimeItemTicket ticket;
  const auto item = Generated("mcp_a");
  Q4T_CHECK(f.history.BeginOutput(item, "unused", true, 8192, &ticket));
  const auto revision = f.history.Revision();
  Q4T_CHECK(!Accepted(
      calls.Begin(ticket, {item, 0, 0}, f.Catalog(), "resp_owned", true)));
  Q4T_CHECK(calls.RetainedBytes() == 0 && calls.Pending() == 0 &&
            f.history.Revision() == revision && f.events.empty());
  return true;
}

Q4T_TEST(realtime_mcp_calls_approval_deadline_abandons_without_tool) {
  Fixture f;
  f.approval = true;
  RealtimeMcpCallLimits limits;
  limits.approval_timeout = 20ms;
  RealtimeMcpCalls calls(f.history, f.Callbacks(), limits);
  RealtimeItemTicket ticket;
  Q4T_CHECK(f.Add(calls, "mcp_a", &ticket) && f.Approval(calls));
  Q4T_CHECK(f.Drain(calls) && f.actions[2] == 0 && f.actions[3] == 1 &&
            f.Shapes());
  return true;
}

Q4T_TEST(realtime_mcp_calls_blocked_worker_keeps_slot_and_queue_expires) {
  Fixture f;
  f.block = static_cast<int>(RealtimeMcpAction::kPropose);
  f.ignore_deadline_while_blocked = true;
  RealtimeMcpCallLimits limits;
  limits.timeout = 20ms;
  RealtimeMcpCalls calls(f.history, f.Callbacks(), limits);
  RealtimeItemTicket first, second;
  Q4T_CHECK(f.Add(calls, "mcp_a", &first) && f.Add(calls, "mcp_b", &second));
  Q4T_CHECK(Accepted(calls.Tick()) && Wait([&] { return f.actions[0] == 1; }));
  std::this_thread::sleep_for(30ms);
  Q4T_CHECK(Accepted(calls.Tick()) && calls.Workers() == 1 &&
            f.actions[0] == 1);
  f.block = -1;
  Q4T_CHECK(f.Drain(calls) && f.actions[0] == 1 && f.actions[2] == 0 &&
            f.Shapes());
  return true;
}

Q4T_TEST(realtime_mcp_calls_failed_in_progress_event_never_dispatches) {
  Fixture f;
  f.fail_event = 0;
  RealtimeMcpCalls calls(f.history, f.Callbacks());
  RealtimeItemTicket ticket;
  Q4T_CHECK(f.Add(calls, "mcp_a", &ticket));
  Q4T_CHECK(Wait([&] { return !Accepted(calls.Tick()); }));
  calls.Cancel();
  calls.Join();
  Q4T_CHECK(f.actions[0] == 1 && f.actions[2] == 0 && f.actions[3] == 1);
  return true;
}

Q4T_TEST(realtime_mcp_calls_foreign_worker_result_does_not_grant_execution) {
  Fixture f;
  f.change = [](RealtimeMcpAction action, ObjectReply* reply) {
    if (action == RealtimeMcpAction::kPropose)
      Set(&reply->value, "name", String("other"));
  };
  RealtimeMcpCalls calls(f.history, f.Callbacks());
  RealtimeItemTicket ticket;
  Q4T_CHECK(f.Add(calls, "mcp_a", &ticket) && f.Drain(calls));
  Q4T_CHECK(f.actions[2] == 0 && f.actions[3] == 1 && f.Shapes());
  return true;
}

Q4T_TEST(realtime_mcp_calls_deleted_approval_revokes_future_dispatch) {
  Fixture f;
  f.approval = true;
  RealtimeMcpCalls calls(f.history, f.Callbacks());
  RealtimeItemTicket ticket, approval;
  Q4T_CHECK(f.Add(calls, "mcp_a", &ticket) && f.Approval(calls));
  Q4T_CHECK(f.history.Find("mcpr_mcp_a", &approval) &&
            Accepted(calls.Erase(approval)));
  Q4T_CHECK(!Accepted(calls.Decide(f.Decision("mcp_a", true))) &&
            f.Drain(calls));
  Q4T_CHECK(f.actions[2] == 0 && f.actions[3] == 1);
  return true;
}

Q4T_TEST(realtime_mcp_calls_join_records_failed_cleanup_without_dispatch) {
  Fixture f;
  f.approval = true;
  f.fail_cleanup = true;
  RealtimeMcpCalls calls(f.history, f.Callbacks());
  RealtimeItemTicket ticket;
  Q4T_CHECK(f.Add(calls, "mcp_a", &ticket) && f.Approval(calls));
  calls.Cancel();
  calls.Join();
  Q4T_CHECK(calls.CleanupFailures() == 1 && !calls.Workers() &&
            calls.RetainedBytes() == 0 && f.actions[2] == 0 && !f.wrong_thread);
  return true;
}

Q4T_TEST(realtime_mcp_calls_delete_terminal_releases_capacity_for_new_call) {
  Fixture f;
  RealtimeMcpCallLimits limits;
  limits.entries = 1;
  RealtimeMcpCalls calls(f.history, f.Callbacks(), limits);
  RealtimeItemTicket ticket;
  Q4T_CHECK(f.Add(calls, "mcp_a", &ticket) && f.Drain(calls));
  Q4T_CHECK(Accepted(calls.Erase(ticket)) && calls.RetainedBytes() == 0);
  Q4T_CHECK(f.Add(calls, "mcp_b", &ticket) && f.Drain(calls) &&
            f.actions[2] == 2);
  return true;
}

Q4T_TEST(realtime_mcp_calls_auxiliary_session_error_keeps_actual_tool_result) {
  Fixture f;
  f.maintenance_failed = true;
  RealtimeMcpCalls calls(f.history, f.Callbacks());
  RealtimeItemTicket ticket;
  Q4T_CHECK(f.Add(calls, "mcp_a", &ticket) && f.Drain(calls));
  Json item;
  Q4T_CHECK(f.history.Get(ticket, &item) &&
            item.GetString("output") == "actual fixture result" &&
            item.Find("error")->IsNull() && f.actions[2] == 1 && f.Shapes());
  return true;
}
