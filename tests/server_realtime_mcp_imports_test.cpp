#include "q4t/server/realtime_mcp_imports.h"

#include <atomic>
#include <stdexcept>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
ResponsesMcpDefinition Definition(std::string url = "https://example.com/mcp") {
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  const auto status = PlanResponsesMcpDefinition(
      Object({{"type", String("mcp")},
              {"server_label", String("project")},
              {"server_url", String(url)},
              {"authorization", String("fixture-private-credential")},
              {"require_approval", String("never")}}),
      &definition, &error);
  if (!status) throw std::runtime_error("Invalid test definition");
  return definition;
}
Json User(std::string text = "Keep answering") {
  return Object({{"type", String("message")},
                 {"role", String("user")},
                 {"content", Array({Object({{"type", String("input_text")},
                                            {"text", String(text)}})})}});
}
Json Empty(std::string id = "mcpl_owned") {
  return Object({{"type", String("mcp_list_tools")},
                 {"id", String(id)},
                 {"server_label", String("project")},
                 {"tools", Array()}});
}
Json Reference(std::string id) {
  return Array(
      {Object({{"type", String("item_reference")}, {"id", String(id)}})});
}
bool Wait(const std::function<bool()>& ready) {
  const auto deadline = Clock::now() + 2s;
  while (!ready()) {
    if (Clock::now() >= deadline) return false;
    std::this_thread::sleep_for(1ms);
  }
  return true;
}
bool Drain(RealtimeMcpImports& imports) {
  bool healthy = true;
  const bool stopped = Wait([&] {
    healthy = Accepted(imports.Tick());
    return !healthy || (!imports.Pending() && !imports.Workers());
  });
  return healthy && stopped;
}
struct Fixture {
  RealtimeConversation history;
  std::vector<Json> events;
  std::atomic<size_t> started{0}, finished{0}, executing{0}, peak{0};
  std::atomic<bool> block{false}, wrong_thread{false}, cancel_observed{false};
  bool empty = false, late_success = false;
  size_t fail_event = SIZE_MAX, next_id = 0;
  ObjectReply reply;
  std::function<void(RealtimeMcpCatalog*)> change;
  const std::thread::id owner = std::this_thread::get_id();
  explicit Fixture(size_t bytes = RealtimeConversation::kMaximumBytes)
      : history("sess_imports", 128, bytes) {}
  RealtimeMcpImportCallbacks Callbacks() {
    RealtimeMcpImportCallbacks callbacks;
    callbacks.new_id = [this](std::string_view prefix) {
      return std::string(prefix) + std::to_string(++next_id);
    };
    callbacks.emit = [this](const Json& event) {
      if (events.size() == fail_event)
        return Error(503, "fixture_sink_failure", "Closed output");
      events.push_back(event);
      return ObjectReply{};
    };
    callbacks.import = [this](const ResponsesMcpDefinition& definition,
                              std::string id, Clock::time_point deadline,
                              const std::function<bool()>& cancelled,
                              RealtimeMcpCatalog* catalog) {
      if (std::this_thread::get_id() == owner) wrong_thread = true;
      ++started;
      const auto active = ++executing;
      auto maximum = peak.load();
      while (maximum < active && !peak.compare_exchange_weak(maximum, active)) {
      }
      while (block && !cancelled() && Clock::now() < deadline)
        std::this_thread::sleep_for(1ms);
      const bool stopped = cancelled();
      if (stopped) cancel_observed = true;
      --executing;
      ++finished;
      if (stopped && !late_success)
        return Error(499, "request_cancelled", "Cancelled test import");
      if (Clock::now() >= deadline)
        return Error(408, "request_timeout", "Expired test import");
      catalog->definition = definition;
      catalog->authority = Empty(id);
      if (!empty)
        Set(&catalog->authority, "tools",
            Array(
                {Object({{"name", String("read")},
                         {"input_schema", Object({{"type", String("object")}})},
                         {"description", String("Read project records")}})}));
      catalog->item = catalog->authority;
      catalog->loaded = true;
      if (change) change(catalog);
      return reply;
    };
    return callbacks;
  }
  std::vector<std::string> Types() const {
    std::vector<std::string> types;
    for (const auto& event : events) types.push_back(event.GetString("type"));
    return types;
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

Q4T_TEST(realtime_mcp_import_pending_catalog_does_not_block_text) {
  Fixture fixture;
  fixture.block = true;
  RealtimeMcpImports imports(fixture.history, fixture.Callbacks());
  RealtimeItemTicket user, ticket;
  Q4T_CHECK(fixture.history.Create(User(), "user", {}, &user));
  Q4T_CHECK(Accepted(imports.Begin(Definition(), "mcpl_owned", true, &ticket)));
  Q4T_CHECK(fixture.started == 0 && imports.Pending() == 1);
  Q4T_CHECK(Accepted(imports.Tick()) &&
            Wait([&] { return fixture.started == 1; }));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(fixture.history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.tickets == std::vector<RealtimeItemTicket>{user});
  bool ready = false;
  Q4T_CHECK(fixture.history.Capture(nullptr, &snapshot, true) &&
            fixture.history.Resolve(&snapshot, &ready) && ready);
  auto input = Reference(ticket.id);
  Q4T_CHECK(!fixture.history.Snapshot(&input, &snapshot));
  RealtimeMcpCatalog catalog;
  Q4T_CHECK(!Accepted(imports.Get(ticket, &catalog)));
  fixture.block = false;
  Q4T_CHECK(Drain(imports) && Accepted(imports.Get(ticket, &catalog)));
  Q4T_CHECK(!fixture.wrong_thread);
  return true;
}

Q4T_TEST(realtime_mcp_import_success_events_and_private_authority_match) {
  Fixture fixture;
  RealtimeMcpImports imports(fixture.history, fixture.Callbacks());
  RealtimeItemTicket ticket, found;
  Q4T_CHECK(Accepted(imports.Begin(Definition(), "mcpl_owned", true, &ticket)));
  Q4T_CHECK(Drain(imports));
  Q4T_CHECK(fixture.Types() ==
            std::vector<std::string>(
                {"conversation.item.added", "mcp_list_tools.in_progress",
                 "conversation.item.done", "mcp_list_tools.completed"}));
  Q4T_CHECK(fixture.Shapes() && imports.Find(Definition(), &found) &&
            found == ticket);
  RealtimeMcpCatalog catalog;
  Q4T_CHECK(Accepted(imports.Get(ticket, &catalog)) && catalog.loaded);
  Q4T_CHECK(catalog.definition.credentials.authorization ==
            "fixture-private-credential");
  Q4T_CHECK(catalog.item.GetArray("tools")->array.size() == 1);
  for (const auto& event : fixture.events)
    Q4T_CHECK(OutputJson(event).find("fixture-private-credential") ==
              std::string::npos);
  Q4T_CHECK(!imports.Find(Definition("https://different.example/mcp"), &found));
  auto changed = Definition();
  changed.credentials.authorization = "changed";
  Q4T_CHECK(!imports.Find(changed, &found));
  return true;
}

Q4T_TEST(realtime_mcp_import_failure_is_empty_terminal_without_authority) {
  Fixture fixture;
  fixture.reply = Error(503, "mcp_remote_error",
                        "remote echoed fixture-private-credential");
  RealtimeMcpImports imports(fixture.history, fixture.Callbacks());
  RealtimeItemTicket ticket;
  Q4T_CHECK(
      Accepted(imports.Begin(Definition(), "mcpl_failed", true, &ticket)));
  Q4T_CHECK(Drain(imports) && fixture.Shapes());
  Q4T_CHECK(fixture.Types().back() == "mcp_list_tools.failed");
  Json item;
  Q4T_CHECK(fixture.history.Get(ticket, &item) &&
            item.GetArray("tools")->array.empty());
  Q4T_CHECK(!item.Has("error") && !item.Has("status"));
  RealtimeMcpCatalog catalog;
  Q4T_CHECK(!Accepted(imports.Get(ticket, &catalog)));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(fixture.history.Snapshot(nullptr, &snapshot));
  for (const auto& event : fixture.events)
    Q4T_CHECK(OutputJson(event).find("fixture-private-credential") ==
              std::string::npos);
  return true;
}

Q4T_TEST(realtime_mcp_import_empty_success_is_distinct_from_failed_list) {
  Fixture fixture;
  fixture.empty = true;
  RealtimeMcpImports imports(fixture.history, fixture.Callbacks());
  RealtimeItemTicket ticket;
  Q4T_CHECK(
      Accepted(imports.Begin(Definition(), "mcpl_empty", false, &ticket)));
  Q4T_CHECK(Drain(imports));
  RealtimeMcpCatalog catalog;
  Q4T_CHECK(Accepted(imports.Get(ticket, &catalog)) && catalog.loaded &&
            catalog.item.GetArray("tools")->array.empty());
  Q4T_CHECK(fixture.Types().back() == "mcp_list_tools.completed");
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(fixture.history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array.empty());
  return true;
}

Q4T_TEST(realtime_mcp_import_mismatched_or_unknown_result_never_grants_tools) {
  for (int counterexample = 0; counterexample < 7; ++counterexample) {
    Fixture fixture;
    fixture.change = [counterexample](RealtimeMcpCatalog* catalog) {
      if (counterexample == 0)
        Set(&catalog->authority, "id", String("mcpl_foreign"));
      if (counterexample == 1)
        Set(&catalog->authority, "server_label", String("foreign"));
      if (counterexample == 2)
        catalog->definition.credentials.authorization = "substitution";
      if (counterexample == 3) Set(&catalog->item, "tools", Array());
      if (counterexample == 4)
        Set(&catalog->authority, "error", String("failed with tools"));
      if (counterexample == 5) catalog->loaded = false;
    };
    if (counterexample == 6) fixture.reply.commit_unknown = true;
    RealtimeMcpImports imports(fixture.history, fixture.Callbacks());
    RealtimeItemTicket ticket;
    Q4T_CHECK(
        Accepted(imports.Begin(Definition(), "mcpl_owned", true, &ticket)));
    Q4T_CHECK(Drain(imports) &&
              fixture.Types().back() == "mcp_list_tools.failed");
    RealtimeMcpCatalog catalog;
    Q4T_CHECK(!Accepted(imports.Get(ticket, &catalog)));
  }
  return true;
}

Q4T_TEST(realtime_mcp_import_delete_and_reuse_ignore_late_success) {
  Fixture fixture;
  fixture.block = true;
  fixture.late_success = true;
  RealtimeMcpImports imports(fixture.history, fixture.Callbacks());
  RealtimeItemTicket old, replacement;
  Q4T_CHECK(Accepted(imports.Begin(Definition(), "mcpl_reused", true, &old)));
  Q4T_CHECK(Accepted(imports.Tick()) &&
            Wait([&] { return fixture.started == 1; }));
  Q4T_CHECK(Accepted(imports.Erase(old)));
  Q4T_CHECK(
      fixture.history.Create(User("replacement"), old.id, {}, &replacement));
  Q4T_CHECK(Drain(imports) && fixture.cancel_observed &&
            imports.Entries() == 0);
  Q4T_CHECK(fixture.events.size() == 2 && replacement != old);
  Json item;
  Q4T_CHECK(fixture.history.Get(replacement, &item) &&
            item.GetString("type") == "message");
  RealtimeMcpCatalog catalog;
  Q4T_CHECK(!Accepted(imports.Get(old, &catalog)) &&
            !imports.Owns(replacement));
  return true;
}

Q4T_TEST(
    realtime_mcp_import_disconnect_cancels_workers_and_never_starts_queue) {
  Fixture fixture;
  fixture.block = true;
  RealtimeMcpImportLimits limits;
  limits.workers = 1;
  RealtimeMcpImports imports(fixture.history, fixture.Callbacks(), limits);
  RealtimeItemTicket a, b;
  Q4T_CHECK(Accepted(imports.Begin(Definition(), "mcpl_a", true, &a)));
  Q4T_CHECK(Accepted(imports.Begin(Definition(), "mcpl_b", true, &b)));
  Q4T_CHECK(Accepted(imports.Tick()) &&
            Wait([&] { return fixture.started == 1; }));
  const auto events = fixture.events.size();
  imports.Cancel();
  imports.Join();
  Q4T_CHECK(fixture.cancel_observed && fixture.started == 1 &&
            fixture.finished == 1);
  Q4T_CHECK(fixture.events.size() == events && fixture.history.Items() == 0);
  Q4T_CHECK(imports.RetainedBytes() == 0 && imports.Entries() == 0 &&
            imports.Workers() == 0);
  Q4T_CHECK(!Accepted(imports.Begin(Definition(), "mcpl_c", true, &a)));
  return true;
}

Q4T_TEST(realtime_mcp_import_worker_and_queue_bounds_reject_without_eviction) {
  Fixture fixture;
  fixture.block = true;
  RealtimeMcpImportLimits limits;
  limits.workers = 2;
  limits.entries = 4;
  RealtimeMcpImports imports(fixture.history, fixture.Callbacks(), limits);
  RealtimeItemTicket ticket;
  for (int i = 0; i < 4; ++i)
    Q4T_CHECK(Accepted(imports.Begin(Definition(), "mcpl_" + std::to_string(i),
                                     true, &ticket)));
  const auto last = ticket;
  Q4T_CHECK(Accepted(imports.Tick()) &&
            Wait([&] { return fixture.started == 2; }));
  Q4T_CHECK(imports.Workers() == 2 && imports.Pending() == 4);
  const auto reply =
      imports.Begin(Definition(), "mcpl_overflow", true, &ticket);
  Q4T_CHECK(reply.http_status == 429 && ticket == last &&
            fixture.history.Items() == 4);
  Q4T_CHECK(fixture.events.size() == 8 && fixture.started == 2);
  fixture.block = false;
  Q4T_CHECK(Drain(imports) && fixture.started == 4 && fixture.peak <= 2);
  return true;
}

Q4T_TEST(realtime_mcp_import_queued_deadline_expires_without_network) {
  Fixture fixture;
  RealtimeMcpImportLimits limits;
  limits.timeout = 1ms;
  RealtimeMcpImports imports(fixture.history, fixture.Callbacks(), limits);
  RealtimeItemTicket ticket;
  Q4T_CHECK(
      Accepted(imports.Begin(Definition(), "mcpl_expired", true, &ticket)));
  std::this_thread::sleep_for(3ms);
  Q4T_CHECK(Drain(imports) && fixture.started == 0);
  Q4T_CHECK(fixture.Types().back() == "mcp_list_tools.failed");
  RealtimeMcpCatalog catalog;
  Q4T_CHECK(!Accepted(imports.Get(ticket, &catalog)));
  return true;
}

Q4T_TEST(realtime_mcp_import_history_preflight_failure_has_no_io_or_events) {
  Fixture fixture(4096);
  RealtimeMcpImports imports(fixture.history, fixture.Callbacks());
  RealtimeItemTicket ticket{"preserved", "old", 9};
  const auto before = ticket;
  Q4T_CHECK(
      !Accepted(imports.Begin(Definition(), "mcpl_capacity", true, &ticket)));
  Q4T_CHECK(ticket == before && fixture.events.empty() && fixture.started == 0);
  Q4T_CHECK(imports.Entries() == 0 && imports.RetainedBytes() == 0 &&
            fixture.history.Items() == 0);
  return true;
}

Q4T_TEST(realtime_mcp_import_large_result_falls_back_to_reserved_failed_item) {
  Fixture fixture(8192);
  fixture.change = [](RealtimeMcpCatalog* catalog) {
    Set(&Field(catalog->authority, "tools").array[0], "description",
        String(std::string(12000, 'x')));
    catalog->item = catalog->authority;
  };
  RealtimeMcpImports imports(fixture.history, fixture.Callbacks());
  RealtimeItemTicket ticket;
  Q4T_CHECK(Accepted(imports.Begin(Definition(), "mcpl_big", true, &ticket)));
  Q4T_CHECK(Drain(imports) &&
            fixture.Types().back() == "mcp_list_tools.failed");
  Q4T_CHECK(fixture.history.ChargedBytes() < 8192);
  Json item;
  Q4T_CHECK(fixture.history.Get(ticket, &item) &&
            item.GetArray("tools")->array.empty());
  RealtimeMcpCatalog catalog;
  Q4T_CHECK(!Accepted(imports.Get(ticket, &catalog)));
  return true;
}

Q4T_TEST(
    realtime_mcp_import_private_storage_limit_never_grants_partial_catalog) {
  Fixture fixture;
  fixture.change = [](RealtimeMcpCatalog* catalog) {
    Set(&Field(catalog->authority, "tools").array[0], "description",
        String(std::string(14000, 'x')));
    catalog->item = catalog->authority;
  };
  RealtimeMcpImportLimits limits;
  limits.retained_bytes = 16384;
  RealtimeMcpImports imports(fixture.history, fixture.Callbacks(), limits);
  RealtimeItemTicket ticket;
  Q4T_CHECK(
      Accepted(imports.Begin(Definition(), "mcpl_private", true, &ticket)));
  const auto charge = imports.RetainedBytes();
  Q4T_CHECK(Drain(imports) &&
            fixture.Types().back() == "mcp_list_tools.failed");
  Q4T_CHECK(imports.RetainedBytes() == charge &&
            charge < limits.retained_bytes);
  RealtimeMcpCatalog catalog;
  Q4T_CHECK(!Accepted(imports.Get(ticket, &catalog)));
  return true;
}

Q4T_TEST(realtime_mcp_import_retire_old_target_never_rebinds_new_definition) {
  Fixture fixture;
  fixture.block = true;
  fixture.late_success = true;
  RealtimeMcpImports imports(fixture.history, fixture.Callbacks());
  RealtimeItemTicket old, fresh, found;
  Q4T_CHECK(Accepted(imports.Begin(Definition(), "mcpl_old", true, &old)));
  Q4T_CHECK(Accepted(imports.Tick()) &&
            Wait([&] { return fixture.started == 1; }));
  Q4T_CHECK(Accepted(imports.Retire(old)) &&
            !imports.Find(Definition(), &found));
  Q4T_CHECK(fixture.Types().back() == "mcp_list_tools.failed");
  const auto next = Definition("https://new.example/mcp");
  Q4T_CHECK(Accepted(imports.Begin(next, "mcpl_new", true, &fresh)));
  fixture.block = false;
  Q4T_CHECK(Drain(imports) && imports.Find(next, &found) && found == fresh);
  RealtimeMcpCatalog catalog;
  Q4T_CHECK(!Accepted(imports.Get(old, &catalog)) &&
            Accepted(imports.Get(fresh, &catalog)));
  Json item;
  Q4T_CHECK(fixture.history.Get(old, &item) &&
            item.GetArray("tools")->array.empty());
  Q4T_CHECK(fixture.Shapes() && imports.Entries() == 1);
  return true;
}

Q4T_TEST(realtime_mcp_import_sink_failure_stops_before_hidden_dispatch) {
  Fixture fixture;
  fixture.fail_event = 1;
  RealtimeMcpImports imports(fixture.history, fixture.Callbacks());
  RealtimeItemTicket ticket;
  Q4T_CHECK(!Accepted(imports.Begin(Definition(), "mcpl_sink", true, &ticket)));
  Q4T_CHECK(!Accepted(imports.Tick()));
  imports.Join();
  Q4T_CHECK(fixture.events.size() == 1 && fixture.started == 0 &&
            fixture.history.Items() == 0);
  Q4T_CHECK(!Accepted(imports.Tick()) && fixture.events.size() == 1);
  return true;
}

Q4T_TEST(realtime_mcp_history_skip_only_owned_pending_catalogs) {
  RealtimeConversation history("sess_catalog");
  RealtimeItemTicket owned, client, function;
  Q4T_CHECK(history.BeginOutput(Empty(), "mcpl_owned", true, 8192, &owned));
  Q4T_CHECK(history.Create(Empty("mcpl_client"), "mcpl_client", {}, &client));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot) &&
            snapshot.tickets == std::vector<RealtimeItemTicket>{client});
  const auto item = Object({{"type", String("function_call")},
                            {"id", String("fn")},
                            {"call_id", String("call_owned")},
                            {"name", String("read")},
                            {"arguments", String("")}});
  Q4T_CHECK(history.BeginOutput(item, "fn", true, 8192, &function));
  Q4T_CHECK(!history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(history.Capture(nullptr, &snapshot, true) &&
            snapshot.pending_outputs == std::vector<size_t>{1});
  return true;
}

Q4T_TEST(realtime_mcp_history_explicit_wait_cannot_substitute_deleted_catalog) {
  RealtimeConversation history("sess_catalog");
  RealtimeItemTicket old, fresh;
  Q4T_CHECK(history.BeginOutput(Empty(), "mcpl_owned", true, 8192, &old));
  auto input = Reference(old.id);
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(!history.Capture(&input, &snapshot));
  Q4T_CHECK(history.Capture(&input, &snapshot, true) &&
            snapshot.pending_outputs == std::vector<size_t>{0});
  Q4T_CHECK(history.Erase(old) &&
            history.Create(Empty(), "mcpl_owned", {}, &fresh));
  bool ready = true;
  Q4T_CHECK(!history.Resolve(&snapshot, &ready) && !ready && old != fresh);
  return true;
}
