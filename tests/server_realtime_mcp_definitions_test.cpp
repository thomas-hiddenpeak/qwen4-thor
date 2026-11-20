#include "q4t/server/realtime_mcp_definitions.h"

#include "../src/server/object_protocol.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
Json Reference(std::string label = "project") {
  return Object(
      {{"type", String("mcp")}, {"server_label", String(std::move(label))}});
}
Json Full(std::string label = "project") {
  auto tool = Reference(std::move(label));
  Set(&tool, "server_url", String("https://tools.example.test/mcp"));
  return tool;
}
Json Private() {
  auto tool = Full();
  Set(&tool, "authorization", String("fixture-private-token"));
  Set(&tool, "headers", Object({{"X-Api-Key", String("fixture-private-key")}}));
  return tool;
}
bool Configure(RealtimeMcpDefinitions* owner, Json tools,
               RealtimeMcpSelection* selected) {
  ChatPlanError error;
  return owner->Prepare(tools, selected, &error) &&
         owner->Commit(selected, &error);
}
}  // namespace

Q4T_TEST(realtime_mcp_label_reuse_requires_committed_session_definition) {
  RealtimeMcpDefinitions owner;
  RealtimeMcpSelection first, reuse;
  ChatPlanError error;
  Q4T_CHECK(owner.Prepare(Array({Private()}), &first, &error));
  Q4T_CHECK(!owner.Prepare(Array({Reference()}), &reuse, &error));
  Q4T_CHECK(error.code == "realtime_mcp_definition_missing");
  Q4T_CHECK(owner.Commit(&first, &error));
  Q4T_CHECK(owner.Prepare(Array({Reference()}), &reuse, &error));
  Q4T_CHECK(reuse.Active().size() == 1 &&
            reuse.Active()[0].credentials.authorization ==
                "fixture-private-token");
  Q4T_CHECK(reuse.Active()[0].target_id == "https://tools.example.test/mcp");
  return true;
}

Q4T_TEST(realtime_mcp_private_credentials_never_enter_public_projection) {
  RealtimeMcpDefinitions owner;
  RealtimeMcpSelection selected;
  const auto request = Array({Private()});
  const auto original = OutputJson(request);
  Q4T_CHECK(Configure(&owner, request, &selected));
  Q4T_CHECK(OutputJson(request) == original);
  const auto projected = OutputJson(selected.Tools());
  Q4T_CHECK(projected.find("fixture-private") == projected.npos &&
            !selected.Tools().array[0].Has("headers") &&
            !selected.Tools().array[0].Has("authorization"));
  Q4T_CHECK(selected.Active()[0].credentials.headers.at("X-Api-Key") ==
            "fixture-private-key");
  return true;
}

Q4T_TEST(realtime_mcp_new_connection_cannot_reuse_or_commit_other_owner) {
  RealtimeMcpDefinitions a, b;
  RealtimeMcpSelection selected, foreign;
  ChatPlanError error;
  Q4T_CHECK(a.Prepare(Array({Private()}), &selected, &error));
  Q4T_CHECK(!b.Commit(&selected, &error) && error.http_status == 409);
  Q4T_CHECK(!b.Prepare(Array({Reference()}), &foreign, &error));
  Q4T_CHECK(b.RetainedServers() == 0 && a.Commit(&selected, &error));
  Q4T_CHECK(!b.Prepare(Array({Reference()}), &foreign, &error));
  return true;
}

Q4T_TEST(realtime_mcp_full_target_replacement_drops_prior_credentials) {
  RealtimeMcpDefinitions owner;
  RealtimeMcpSelection previous, replaced;
  Q4T_CHECK(Configure(&owner, Array({Private()}), &previous));
  for (const auto* url : {"https://tools.example.test/mcp",
                          "https://different.example.test/mcp"}) {
    auto tool = Full();
    Set(&tool, "server_url", String(url));
    Q4T_CHECK(Configure(&owner, Array({tool}), &replaced));
    Q4T_CHECK(replaced.Active()[0].credentials.authorization.empty() &&
              replaced.Active()[0].credentials.headers.empty());
    Q4T_CHECK(previous.Active()[0].credentials.authorization ==
              "fixture-private-token");
  }
  return true;
}

Q4T_TEST(realtime_mcp_reference_overrides_keep_target_and_narrow_policy) {
  RealtimeMcpDefinitions owner;
  RealtimeMcpSelection initial, selected;
  Q4T_CHECK(Configure(&owner, Array({Private()}), &initial));
  auto tool = Reference();
  Set(&tool, "allowed_tools", Array({String("read")}));
  Set(&tool, "require_approval", String("always"));
  Q4T_CHECK(Configure(&owner, Array({tool}), &selected));
  const auto& definition = selected.Active()[0];
  Q4T_CHECK(definition.target_id == initial.Active()[0].target_id &&
            definition.credentials.authorization == "fixture-private-token");
  Q4T_CHECK(ResponsesMcpToolAllowed(definition.declaration,
                                    Object({{"name", String("read")}})));
  Q4T_CHECK(!ResponsesMcpToolAllowed(definition.declaration,
                                     Object({{"name", String("write")}})));
  Q4T_CHECK(ResponsesMcpApprovalRequired(definition.declaration,
                                         Object({{"name", String("read")}})));
  return true;
}

Q4T_TEST(realtime_mcp_explicit_clear_removes_authorization_and_headers) {
  RealtimeMcpDefinitions owner;
  RealtimeMcpSelection selected;
  Q4T_CHECK(Configure(&owner, Array({Private()}), &selected));
  auto clear = Reference();
  Set(&clear, "authorization", String(""));
  Set(&clear, "headers", Json());
  Q4T_CHECK(Configure(&owner, Array({clear}), &selected));
  Q4T_CHECK(Configure(&owner, Array({Reference()}), &selected));
  Q4T_CHECK(selected.Active()[0].credentials.authorization.empty() &&
            selected.Active()[0].credentials.headers.empty());
  return true;
}

Q4T_TEST(realtime_mcp_duplicate_label_update_is_atomic) {
  RealtimeMcpDefinitions owner;
  RealtimeMcpSelection selected;
  ChatPlanError error;
  Q4T_CHECK(Configure(&owner, Array({Private()}), &selected));
  const auto previous = OutputJson(selected.Tools());
  Q4T_CHECK(!owner.Prepare(Array({Full("other"), Full(), Reference()}),
                           &selected, &error));
  Q4T_CHECK(error.param == "tools[2].server_label");
  Q4T_CHECK(owner.RetainedServers() == 1 &&
            OutputJson(selected.Tools()) == previous);
  Q4T_CHECK(!owner.Prepare(Array({Reference("other")}), &selected, &error));
  return true;
}

Q4T_TEST(realtime_mcp_invalid_target_and_credential_updates_preserve_state) {
  RealtimeMcpDefinitions owner;
  RealtimeMcpSelection selected;
  ChatPlanError error;
  Q4T_CHECK(Configure(&owner, Array({Private()}), &selected));
  for (const auto& patch :
       {Object({{"server_url", String("file:///private")}}),
        Object({{"server_url", Json()}}), Object({{"authorization", Json()}}),
        Object({{"headers", Object({{"Authorization", String("conflict")}})}}),
        Object({{"server_url", String("https://new.example.test/mcp")},
                {"tunnel_id", String("tunnel_conflict")}})}) {
    auto tool = Reference();
    for (const auto& [key, value] : patch.object) Set(&tool, key, value);
    Q4T_CHECK(!owner.Prepare(Array({Full("other"), tool}), &selected, &error));
    Q4T_CHECK(error.http_status == 400 && owner.RetainedServers() == 1);
    Q4T_CHECK(owner.Prepare(Array({Reference()}), &selected, &error));
    Q4T_CHECK(selected.Active()[0].credentials.authorization ==
              "fixture-private-token");
  }
  return true;
}

Q4T_TEST(realtime_mcp_stale_and_repeated_commit_cannot_overwrite_new_state) {
  RealtimeMcpDefinitions owner;
  RealtimeMcpSelection stale, current, reuse;
  ChatPlanError error;
  Q4T_CHECK(owner.Prepare(Array({Private()}), &stale, &error));
  auto changed = Private();
  Set(&changed, "authorization", String("fixture-rotated-token"));
  Q4T_CHECK(Configure(&owner, Array({changed}), &current));
  Q4T_CHECK(!owner.Commit(&stale, &error) && error.http_status == 409);
  Q4T_CHECK(!owner.Commit(&current, &error) && error.http_status == 409);
  Q4T_CHECK(owner.Prepare(Array({Reference()}), &reuse, &error));
  Q4T_CHECK(reuse.Active()[0].credentials.authorization ==
            "fixture-rotated-token");
  return true;
}

Q4T_TEST(realtime_mcp_empty_active_list_remembers_bounded_labels) {
  RealtimeMcpDefinitions owner;
  RealtimeMcpSelection selected;
  Q4T_CHECK(Configure(&owner, Array({Private()}), &selected));
  Q4T_CHECK(Configure(&owner, Array(), &selected));
  Q4T_CHECK(selected.Tools().array.empty() && selected.Active().empty() &&
            owner.RetainedServers() == 1);
  Q4T_CHECK(Configure(&owner, Array({Reference()}), &selected));
  Q4T_CHECK(selected.Active()[0].credentials.authorization ==
            "fixture-private-token");
  return true;
}

Q4T_TEST(realtime_mcp_count_capacity_preserves_existing_definitions) {
  RealtimeMcpDefinitions owner(1);
  RealtimeMcpSelection selected;
  ChatPlanError error;
  Q4T_CHECK(Configure(&owner, Array({Private()}), &selected));
  Q4T_CHECK(Configure(&owner, Array(), &selected));
  Q4T_CHECK(!owner.Prepare(Array({Full("second")}), &selected, &error));
  Q4T_CHECK(error.http_status == 413 && owner.RetainedServers() == 1);
  Q4T_CHECK(Configure(&owner, Array({Reference()}), &selected));
  Q4T_CHECK(selected.Active()[0].credentials.authorization ==
            "fixture-private-token");
  return true;
}

Q4T_TEST(realtime_mcp_storage_capacity_counts_private_headers) {
  RealtimeMcpDefinitions owner(2, 16384);
  RealtimeMcpSelection selected;
  ChatPlanError error;
  Q4T_CHECK(Configure(&owner, Array({Full()}), &selected));
  auto large = Full("large");
  Set(&large, "headers", Object({{"X-Data", String(std::string(8192, 'x'))}}));
  const auto input = Array({large});
  size_t bytes = 0;
  Q4T_CHECK(MeasureRealtimeStorage(input, 16384, &bytes));
  Q4T_CHECK(!owner.Prepare(input, &selected, &error));
  Q4T_CHECK(error.http_status == 413 && owner.RetainedServers() == 1);
  Q4T_CHECK(owner.RetainedBytes() <= 16384);
  Q4T_CHECK(Configure(&owner, Array({Reference()}), &selected));
  return true;
}

Q4T_TEST(
    realtime_mcp_connector_and_tunnel_targets_preserve_deployment_boundary) {
  RealtimeMcpDefinitions owner;
  RealtimeMcpSelection selected;
  ChatPlanError error;
  auto connector = Reference();
  Set(&connector, "connector_id", String("connector_googledrive"));
  Set(&connector, "headers",
      Object({{"aUtHoRiZaTiOn", String("Bearer fixture-private")}}));
  Q4T_CHECK(!owner.Prepare(Array({connector}), &selected, &error));
  Q4T_CHECK(error.param == "tools[0].headers");
  Set(&connector, "headers", Json());
  Set(&connector, "authorization", String("fixture-private-token"));
  Q4T_CHECK(Configure(&owner, Array({connector}), &selected));
  Q4T_CHECK(selected.Active()[0].credentials.url.empty() &&
            selected.Active()[0].target_binding.empty());
  auto tunnel = Reference();
  Set(&tunnel, "tunnel_id", String("tunnel_project"));
  Q4T_CHECK(Configure(&owner, Array({tunnel}), &selected));
  const auto& definition = selected.Active()[0];
  Q4T_CHECK(definition.target_kind == "tunnel_id" &&
            definition.target_id == "tunnel_project" &&
            definition.credentials.url.empty() &&
            definition.credentials.tunnel_id.empty() &&
            definition.credentials.authorization.empty());
  return true;
}

Q4T_TEST(realtime_mcp_function_passthrough_cannot_seed_mcp_labels) {
  RealtimeMcpDefinitions owner;
  RealtimeMcpSelection selected;
  ChatPlanError error;
  auto function =
      Object({{"name", String("project")},
              {"parameters",
               Object({{"type", String("object")},
                       {"description", String("authorization data")}})}});
  Q4T_CHECK(Configure(&owner, Array({function}), &selected));
  Q4T_CHECK(OutputJson(selected.Tools().array[0]) == OutputJson(function) &&
            selected.Active().empty() && owner.RetainedServers() == 0);
  Q4T_CHECK(!owner.Prepare(Array({Reference()}), &selected, &error));
  Q4T_CHECK(error.code == "realtime_mcp_definition_missing");
  Set(&function, "parameters", String("invalid schema"));
  Q4T_CHECK(!owner.Prepare(Array({function}), &selected, &error));
  return true;
}
