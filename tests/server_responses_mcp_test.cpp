#include "q4t/server/responses_mcp.h"

#include "../src/server/object_protocol.h"
#include "q4t/server/responses_program.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
Json Value(const std::string& text) {
  Json value;
  if (!Parse(text, &value)) throw std::runtime_error("MCP contract fixture");
  return value;
}
Json Declaration() {
  return Value(R"({"type":"mcp","server_label":"project docs",
                   "server_url":"https://tools.example.com/mcp"})");
}
Json Tool(std::string name = "read", bool read_only = true) {
  auto value = Value(R"({"name":"read","description":"Read project data",
    "inputSchema":{"type":"object","properties":{"path":{"type":"string"}},
      "required":["path"],"additionalProperties":false},
    "outputSchema":{"type":"object","properties":{"text":{"type":"string"}},
      "required":["text"],"additionalProperties":false},
    "annotations":{"readOnlyHint":true}})");
  Set(&value, "name", String(std::move(name)));
  Set(&Field(value, "annotations"), "readOnlyHint", Boolean(read_only));
  return value;
}
}  // namespace

Q4T_TEST(responses_mcp_capture_is_typed_and_conflicts_never_partially_publish) {
  auto tool = Declaration();
  Set(&tool, "authorization", String("request-private-token"));
  auto request = Object(
      {{"tools", Array({tool})},
       {"input",
        Array({Object({{"type", String("additional_tools")},
                       {"role", String("developer")},
                       {"tools", Array({tool})}}),
               Object({{"type", String("function_call_output")},
                       {"call_id", String("client_result")},
                       {"output",
                        String("{\"authorization\":\"user data\"}")}})})}});
  std::vector<ResponsesMcpDefinition> credentials;
  ChatPlanError error;
  Q4T_CHECK(CaptureResponsesMcpCredentials(&request, &credentials, &error));
  Q4T_CHECK(credentials.size() == 1 &&
            ResponsesMcpHasCredentials(credentials[0]));
  const auto clean = OutputJson(request);
  Q4T_CHECK(clean.find("request-private-token") == clean.npos &&
            clean.find("user data") != clean.npos);
  ResponsesMcpDefinition rebound;
  Q4T_CHECK(PlanResponsesMcpDefinition(request.GetArray("tools")->array[0],
                                       &rebound, &error));
  BindResponsesMcpCredentials(&rebound, credentials);
  Q4T_CHECK(rebound.credentials.authorization == "request-private-token");
  auto other = tool;
  Set(&other, "authorization", String("different-private-token"));
  request = Object({{"tools", Array({tool, other})}});
  const auto before = OutputJson(request);
  Q4T_CHECK(!CaptureResponsesMcpCredentials(&request, &credentials, &error));
  Q4T_CHECK(OutputJson(request) == before && error.param == "tools[1]");
  return true;
}

Q4T_TEST(responses_mcp_credentials_are_request_local_not_echoed) {
  auto input = Declaration();
  Set(&input, "authorization", String("private-fixture-token"));
  Set(&input, "headers",
      Object({{"X-Api-Key", String("private-fixture-key")}}));
  const auto original = OutputJson(input);
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpDefinition(input, &definition, &error, "tools[2]"));
  Q4T_CHECK(OutputJson(input) == original);
  Q4T_CHECK(definition.credentials.authorization == "private-fixture-token");
  Q4T_CHECK(definition.credentials.headers.at("X-Api-Key") ==
            "private-fixture-key");
  Q4T_CHECK(!definition.declaration.Find("authorization") &&
            !definition.declaration.Find("headers"));
  Q4T_CHECK(OutputJson(definition.declaration).find("private-fixture") ==
            std::string::npos);
  ResponsesMcpDefinition continued;
  Q4T_CHECK(
      PlanResponsesMcpDefinition(definition.declaration, &continued, &error));
  Q4T_CHECK(continued.credentials.authorization.empty() &&
            continued.credentials.headers.empty());
  return true;
}

Q4T_TEST(responses_mcp_definition_fields_nulls_and_targets_checked) {
  for (const auto* patch :
       {R"({"server_url":null})", R"({"tunnel_id":"tunnel_x"})",
        R"({"authorization":null})", R"({"headers":{"X-Key":3}})",
        R"({"headers":{"Host":"other"}})",
        R"({"authorization":"a","headers":{"Authorization":"b"}})",
        R"({"defer_loading":null})", R"({"allowed_tools":{"read_only":null}})",
        R"({"require_approval":{"sometimes":{}}})",
        R"({"require_approval":{"never":{"tool_names":[null]}}})",
        R"({"server_url":"file:///etc/passwd"})",
        R"({"allowed_callers":["direct","direct"]})"}) {
    auto input = Declaration();
    for (auto& [key, value] : Value(patch).object) Set(&input, key, value);
    ResponsesMcpDefinition definition;
    ChatPlanError error;
    Q4T_CHECK(
        !PlanResponsesMcpDefinition(input, &definition, &error, "tools[2]"));
    Q4T_CHECK(error.http_status == 400 && error.param.starts_with("tools[2]"));
    Q4T_CHECK(definition.declaration.IsNull());
  }
  auto input = Declaration();
  for (const auto* field :
       {"headers", "allowed_tools", "require_approval", "allowed_callers"})
    Set(&input, field, Json{});
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpDefinition(input, &definition, &error));
  Q4T_CHECK(ResponsesMcpToolAllowed(definition.declaration, Tool()));
  Q4T_CHECK(ResponsesMcpApprovalRequired(definition.declaration, Tool()));
  return true;
}

Q4T_TEST(responses_mcp_allowed_names_and_readonly_filter_intersect) {
  auto input = Declaration();
  Set(&input, "allowed_tools",
      Value(R"({"tool_names":["read","read"],"read_only":true})"));
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpDefinition(input, &definition, &error));
  Q4T_CHECK(ResponsesMcpToolAllowed(input, Tool()));
  Q4T_CHECK(!ResponsesMcpToolAllowed(input, Tool("write", true)));
  Q4T_CHECK(!ResponsesMcpToolAllowed(input, Tool("read", false)));
  auto unknown = Tool();
  Set(&unknown, "annotations", Object());
  Q4T_CHECK(!ResponsesMcpToolAllowed(input, unknown));
  Set(&input, "allowed_tools", Value(R"({"read_only":false})"));
  Q4T_CHECK(ResponsesMcpToolAllowed(input, unknown));
  Set(&input, "allowed_tools", Array());
  Q4T_CHECK(!ResponsesMcpToolAllowed(input, Tool()));
  Set(&input, "allowed_tools", Object());
  Q4T_CHECK(ResponsesMcpToolAllowed(input, Tool()));
  return true;
}

Q4T_TEST(responses_mcp_default_and_explicit_approval_policy) {
  auto input = Declaration();
  Q4T_CHECK(ResponsesMcpApprovalRequired(input, Tool()));
  Set(&input, "require_approval", String("never"));
  Q4T_CHECK(!ResponsesMcpApprovalRequired(input, Tool()));
  Set(&input, "require_approval", String("always"));
  Q4T_CHECK(ResponsesMcpApprovalRequired(input, Tool()));
  Set(&input, "require_approval", Value(R"({
    "never":{"read_only":true},"always":{"tool_names":["private"]}})"));
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpDefinition(input, &definition, &error));
  Q4T_CHECK(!ResponsesMcpApprovalRequired(input, Tool("read", true)));
  Q4T_CHECK(ResponsesMcpApprovalRequired(input, Tool("private", true)));
  Q4T_CHECK(ResponsesMcpApprovalRequired(input, Tool("write", false)));
  return true;
}

Q4T_TEST(responses_mcp_catalog_preserves_original_result_contract_privately) {
  auto input = Declaration();
  Set(&input, "allowed_tools", Array({String("read")}));
  Set(&input, "defer_loading", Boolean(true));
  Set(&input, "allowed_callers", Array({String("programmatic")}));
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpDefinition(input, &definition, &error));
  Json projected, authority;
  const auto original = Tool();
  Q4T_CHECK(ProjectResponsesMcpCatalog(definition,
                                       Array({original, Tool("hidden")}),
                                       &projected, &authority, &error));
  Q4T_CHECK(projected.array.size() == 1 && authority.array.size() == 1);
  Q4T_CHECK(OutputJson(authority.array[0]) == OutputJson(original));
  Q4T_CHECK(projected.array[0].Find("input_schema") &&
            !projected.array[0].Find("outputSchema"));
  Q4T_CHECK(OutputJson(*projected.array[0].Find("input_schema")) ==
            OutputJson(*original.Find("inputSchema")));
  Q4T_CHECK(ResponsesToolAllows(definition.declaration, "programmatic") &&
            !ResponsesToolAllows(definition.declaration, "direct") &&
            definition.declaration.GetBool("defer_loading"));
  return true;
}

Q4T_TEST(responses_mcp_catalog_rejects_malformed_remote_authority) {
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpDefinition(Declaration(), &definition, &error));
  for (const auto* patch :
       {R"({"inputSchema":{"type":"array"}})", R"({"outputSchema":null})",
        R"({"description":2})", R"({"annotations":{"readOnlyHint":"true"}})"}) {
    auto tool = Tool();
    for (const auto& [key, value] : Value(patch).object) Set(&tool, key, value);
    Json projected, authority;
    Q4T_CHECK(!ProjectResponsesMcpCatalog(definition, Array({tool}), &projected,
                                          &authority, &error));
    Q4T_CHECK(projected.array.empty() && authority.array.empty());
  }
  Json projected, authority;
  Q4T_CHECK(!ProjectResponsesMcpCatalog(definition, Array({Tool(), Tool()}),
                                        &projected, &authority, &error));
  return true;
}

Q4T_TEST(responses_mcp_connector_and_tunnel_contracts_do_not_invent_endpoint) {
  for (const auto* field : {"connector_id", "tunnel_id"}) {
    auto input = Value(R"({"type":"mcp","server_label":"project"})");
    Set(&input, field,
        String(std::string(field) == "connector_id" ? "connector_googledrive"
                                                    : "tunnel_deployed"));
    ResponsesMcpDefinition definition;
    ChatPlanError error;
    Q4T_CHECK(PlanResponsesMcpDefinition(input, &definition, &error));
    Q4T_CHECK(definition.target_kind == field &&
              definition.credentials.url.empty());
  }
  return true;
}
