#include "q4t/server/realtime_mcp_context.h"

#include "../src/server/object_protocol.h"
#include "q4t/server/realtime_chat_plan.h"
#include "q4t/server/realtime_mcp_history.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
Json Tool(std::string label = "docs", std::string url = "https://example.com") {
  return Object({{"type", String("mcp")},
                 {"server_label", String(label)},
                 {"server_url", String(url)},
                 {"authorization", String("context-fixture-token")},
                 {"headers", Object({{"x-fixture", String("private-value")}})},
                 {"require_approval", String("never")}});
}
RealtimeMcpCatalog Catalog(const ResponsesMcpDefinition& definition) {
  RealtimeMcpCatalog result;
  result.definition = definition;
  result.item = Object(
      {{"type", String("mcp_list_tools")},
       {"id",
        String("mcpl_" + definition.declaration.GetString("server_label"))},
       {"server_label", *definition.declaration.Find("server_label")},
       {"tools", Array({Object({{"name", String("read")},
                                {"input_schema",
                                 Object({{"type", String("object")}})}})})}});
  result.authority = result.item;
  result.loaded = true;
  return result;
}
struct Fixture {
  RealtimeMcpDefinitions owner;
  RealtimeMcpSelection selected;
  std::shared_ptr<const RealtimeMcpContext> context;
  ChatPlanError error;
  ChatPlanConfig config;
  Fixture() {
    config.model = "fixture";
    config.max_len = 8192;
    config.structured = true;
  }
  bool Capture(bool loaded = true, Json tools = Array({Tool()})) {
    if (!owner.Prepare(tools, &selected, &error)) return false;
    std::vector<RealtimeMcpCatalog> catalogs;
    if (loaded)
      for (const auto& definition : selected.Active())
        catalogs.push_back(Catalog(definition));
    return CaptureRealtimeMcpContext(selected, catalogs, &context, &error).ok();
  }
  bool Plan(Json choice, RealtimeChatPlan* plan, bool preview = false) {
    auto settings = DefaultRealtimeChatSettings(config, "");
    Set(&settings, "tools", context->tools);
    Set(&settings, "tool_choice", std::move(choice));
    return PlanRealtimeChat(settings, Object(), Array(), config, plan, &error,
                            nullptr, false, context.get(), preview)
        .ok();
  }
};
Json Call(bool failure = false) {
  auto call = Object({{"id", String("mcp_fixture")},
                      {"type", String("mcp_call")},
                      {"server_label", String("docs")},
                      {"name", String("read")},
                      {"arguments", String("{}")}});
  if (!failure)
    Set(&call, "output", String("actual owned result"));
  else
    Set(&call, "error",
        Object({{"type", String("protocol_error")},
                {"code", Number(-32603)},
                {"message", String("cancelled before dispatch")}}));
  return call;
}
}  // namespace

Q4T_TEST(realtime_mcp_context_freezes_private_credentials_and_model_names) {
  Fixture f;
  Q4T_CHECK(f.Capture() && f.context->bindings.size() == 1);
  Q4T_CHECK(OutputJson(f.context->tools).find("context-fixture-token") ==
            std::string::npos);
  RealtimeChatPlan plan;
  Q4T_CHECK(f.Plan(Object({{"type", String("mcp")},
                           {"server_label", String("docs")},
                           {"name", String("read")}}),
                   &plan));
  const auto& request = plan.generation.generation_request;
  const auto name = ResponsesMcpModelName("docs", "read");
  Q4T_CHECK(
      request.GetArray("tools")->array[0].Find("function")->GetString("name") ==
      name);
  Q4T_CHECK(request.Find("tool_choice")->Find("function")->GetString("name") ==
            name);
  Q4T_CHECK(OutputJson(request).find("private-value") == std::string::npos);
  Q4T_CHECK(f.owner.Commit(&f.selected, &f.error));
  RealtimeMcpSelection changed;
  Q4T_CHECK(f.owner.Prepare(Array({Tool("docs", "https://other.example")}),
                            &changed, &f.error));
  Q4T_CHECK(f.owner.Commit(&changed, &f.error));
  Q4T_CHECK(f.context->catalogs[0].definition.target_id ==
                "https://example.com" &&
            f.context->catalogs[0].definition.credentials.authorization ==
                "context-fixture-token");
  return true;
}
Q4T_TEST(realtime_mcp_context_missing_import_allows_auto_but_not_forced_tool) {
  Fixture f;
  Q4T_CHECK(f.Capture(false));
  RealtimeChatPlan plan;
  Q4T_CHECK(f.Plan(String("auto"), &plan));
  Q4T_CHECK(!f.Plan(
      Object({{"type", String("mcp")}, {"server_label", String("docs")}}),
      &plan));
  Q4T_CHECK(!f.Plan(String("required"), &plan));
  Q4T_CHECK(f.Plan(String("required"), &plan, true));
  Q4T_CHECK(f.Plan(
      Object({{"type", String("mcp")}, {"server_label", String("docs")}}),
      &plan, true));
  return true;
}
Q4T_TEST(realtime_mcp_context_rejects_target_credentials_and_catalog_changes) {
  Fixture f;
  Q4T_CHECK(f.Capture());
  const auto old = f.context;
  for (int mode = 0; mode < 4; ++mode) {
    auto catalog = Catalog(f.selected.Active()[0]);
    if (mode == 0) catalog.definition.target_id = "https://other.example";
    if (mode == 1) catalog.definition.credentials.authorization = "changed";
    if (mode == 2) catalog.loaded = false;
    if (mode == 3) Set(&catalog.item, "id", String("mcpl_changed"));
    Q4T_CHECK(!CaptureRealtimeMcpContext(f.selected, {catalog}, &f.context,
                                         &f.error));
    Q4T_CHECK(f.context == old);
  }
  return true;
}
Q4T_TEST(realtime_mcp_context_no_private_alias_selector_or_duplicate_catalog) {
  Fixture f;
  Q4T_CHECK(f.Capture());
  RealtimeChatPlan plan;
  for (const auto& name : {f.context->bindings[0].model_name,
                           f.context->bindings[0].function.GetString("name")})
    Q4T_CHECK(!f.Plan(
        Object({{"type", String("function")}, {"name", String(name)}}), &plan));
  const auto catalog = Catalog(f.selected.Active()[0]);
  Q4T_CHECK(!CaptureRealtimeMcpContext(f.selected, {catalog, catalog},
                                       &f.context, &f.error));
  return true;
}
Q4T_TEST(realtime_mcp_context_mixed_functions_and_same_named_remote_tools) {
  Fixture f;
  auto tools =
      Array({Tool(), Tool("second", "https://second.example"),
             Object({{"name", String("read")},
                     {"parameters", Object({{"type", String("object")}})}})});
  Q4T_CHECK(f.Capture(true, tools));
  RealtimeChatPlan plan;
  Q4T_CHECK(f.Plan(
      Object({{"type", String("function")}, {"name", String("read")}}), &plan));
  Q4T_CHECK(
      plan.generation.generation_request.GetArray("tools")->array.size() == 3);
  Q4T_CHECK(f.context->bindings[0].model_name !=
            f.context->bindings[1].model_name);
  Q4T_CHECK(plan.generation.generation_request.Find("tool_choice")
                ->Find("function")
                ->GetString("name") == "read");
  return true;
}
Q4T_TEST(realtime_mcp_context_request_mismatch_is_atomic) {
  Fixture f;
  Q4T_CHECK(f.Capture());
  auto request = Object({{"tools", Array()}, {"tool_choice", String("auto")}});
  const auto original = OutputJson(request);
  Q4T_CHECK(!MapRealtimeMcpTools(*f.context, &request, false, &f.error));
  Q4T_CHECK(OutputJson(request) == original);
  return true;
}
Q4T_TEST(realtime_mcp_history_client_copy_has_no_local_provenance) {
  RealtimeConversation history("sess_proof");
  RealtimeItemTicket ticket;
  const auto call = Call();
  Q4T_CHECK(history.BeginOutput(call, "unused", true, 8192, &ticket));
  Q4T_CHECK(history.Update(ticket, call, true));
  RealtimeConversationSnapshot owned, copied;
  Q4T_CHECK(history.Snapshot(nullptr, &owned) &&
            owned.owned_mcp == std::vector<bool>{true});
  const auto direct = Array({call});
  Q4T_CHECK(history.Snapshot(&direct, &copied) &&
            copied.owned_mcp == std::vector<bool>{false});
  Json native;
  ChatPlanConfig config;
  const StateAccess access{
      "client:fixture",
      std::chrono::steady_clock::now() + std::chrono::seconds(1),
      {}};
  Q4T_CHECK(
      ResolveRealtimeMcpHistory(nullptr, config, owned, access, &native).ok());
  Q4T_CHECK(!ResolveRealtimeMcpHistory(nullptr, config, copied, access, &native)
                 .ok());
  return true;
}
Q4T_TEST(realtime_mcp_history_local_cancel_needs_no_auxiliary_store) {
  RealtimeConversationSnapshot snapshot;
  auto item = Call(true);
  Set(&item, "arguments", String("{\"path\":"));
  snapshot.items = Array({item});
  snapshot.owned_mcp = {true};
  Json native;
  ChatPlanConfig config;
  const StateAccess access{
      "client:fixture",
      std::chrono::steady_clock::now() + std::chrono::seconds(1),
      {}};
  Q4T_CHECK(
      ResolveRealtimeMcpHistory(nullptr, config, snapshot, access, &native)
          .ok());
  Q4T_CHECK(native.array[0].GetString("status") == "incomplete");
  const auto old = OutputJson(native);
  snapshot.owned_mcp.push_back(true);
  Q4T_CHECK(
      !ResolveRealtimeMcpHistory(nullptr, config, snapshot, access, &native)
           .ok());
  Q4T_CHECK(OutputJson(native) == old);
  return true;
}
Q4T_TEST(realtime_mcp_context_history_and_truncation_keep_public_indices) {
  Fixture f;
  Q4T_CHECK(f.Capture());
  auto settings = DefaultRealtimeChatSettings(f.config, "");
  Set(&settings, "tools", f.context->tools);
  Set(&settings, "truncation",
      Object({{"type", String("retention_ratio")},
              {"retention_ratio", Number(0.8)}}));
  const auto history = Array({Call()});
  Json native;
  Q4T_CHECK(PreviewRealtimeMcpHistory(history, &native).ok());
  RealtimeChatPlan plan;
  Q4T_CHECK(PlanRealtimeChat(settings, Object(), history, f.config, &plan,
                             &f.error, nullptr, false, f.context.get(), false,
                             &native));
  Q4T_CHECK(RealtimeRetainedHistory(plan) == std::vector<size_t>{0});
  Q4T_CHECK(OutputJson(plan.generation.generation_request)
                .find("actual owned result") != std::string::npos);
  Q4T_CHECK(FitRealtimeChatContext(
      f.config,
      [](const auto&, size_t* tokens, auto*) {
        *tokens = 12;
        return q4t::Status{};
      },
      &plan, &f.error));
  return true;
}
