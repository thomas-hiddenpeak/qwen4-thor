#include "q4t/server/mcp_policy.h"

#include <sys/stat.h>
#include <unistd.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "../src/server/object_protocol.h"
#include "q4t/server/responses_mcp.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
namespace fs = std::filesystem;
constexpr const char* kPolicy = R"({"version":1,
  "fetch":{"public_networks":false,"allow_http":false,
    "allowed_networks":["10.1.0.0/16"],"allowed_hosts":["private.example"],
    "ca_file":"/private/trust.pem","ports":[443]},
  "targets":[{"kind":"connector_id","id":"connector_googledrive",
    "url":"https://private.example/mcp?private_route=value",
    "generation":"approved-1","scopes":["client:owner"]},
    {"kind":"tunnel_id","id":"tunnel_project",
    "url":"https://private.example/private-mcp","generation":"tunnel-1",
    "scopes":["client:owner"]}]})";
ResponsesMcpDefinition Definition(std::string kind = "connector_id",
                                  std::string id = "connector_googledrive") {
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  if (!PlanResponsesMcpDefinition(
          Object({{"type", String("mcp")},
                  {"server_label", String("project")},
                  {kind, String(id)},
                  {"authorization", String("user-token")}}),
          &definition, &error))
    throw std::runtime_error("MCP policy definition fixture");
  return definition;
}
}  // namespace

Q4T_TEST(mcp_policy_offline_parse_and_redacted_discovery) {
  McpDeploymentPolicy policy;
  Q4T_CHECK(ParseMcpDeploymentPolicy(R"({"version":1})", &policy));
  Q4T_CHECK(policy.targets.empty() && policy.fetch.max_redirects == 0);
  Q4T_CHECK(ParseMcpDeploymentPolicy(kPolicy, &policy));
  Q4T_CHECK(policy.targets.size() == 2 && !policy.fetch.public_networks &&
            !policy.fetch.allow_http && policy.fetch.max_redirects == 0);
  const auto description = McpDeploymentPolicyDescription(&policy);
  Q4T_CHECK(description.GetInt("target_count") == 2 &&
            description.GetString("sha256") == Digest(kPolicy));
  const auto text = OutputJson(description);
  for (const auto* hidden : {"private.example", "trust.pem", "10.1.0.0",
                             "private_route", "client:owner"})
    Q4T_CHECK(text.find(hidden) == text.npos);
  Q4T_CHECK(!McpDeploymentPolicyDescription(nullptr).GetBool("configured"));
  return true;
}

Q4T_TEST(
    mcp_policy_rejects_ambiguous_unsafe_or_unbounded_routes_transactionally) {
  McpDeploymentPolicy policy;
  Q4T_CHECK(ParseMcpDeploymentPolicy(kPolicy, &policy));
  for (const auto* raw :
       {"{}", "[]", R"({"version":1,"version":1})", R"({"version":1.0})",
        R"({"version":2})", R"({"version":1,"fetch":{"max_redirects":1}})",
        R"({"version":1,"fetch":{"ca_file":"relative"}})",
        R"({"version":1,"fetch":{"proxy":"secret"}})",
        R"({"version":1,"targets":null})"}) {
    Q4T_CHECK(!ParseMcpDeploymentPolicy(raw, &policy));
    Q4T_CHECK(policy.sha256 == Digest(kPolicy));
  }
  Json valid;
  Q4T_CHECK(Parse(kPolicy, &valid));
  for (const auto& patch :
       {Object({{"kind", String("server_url")}}),
        Object({{"id", String("connector_unknown")}}),
        Object({{"url", String("file:///private")}}),
        Object({{"url", String("https://user:secret@host/")}}),
        Object({{"url", String("http://private.example/")}}),
        Object({{"scopes", Array()}}),
        Object({{"scopes", Array({String("*")})}}),
        Object({{"scopes", Array({String("client:a"), String("client:a")})}}),
        Object({{"generation", String("")}}),
        Object({{"authorization", String("embedded-secret")}})}) {
    auto changed = valid;
    for (const auto& [key, value] : patch.object)
      Set(&Field(changed, "targets").array[0], key, value);
    Q4T_CHECK(!ParseMcpDeploymentPolicy(OutputJson(changed), &policy));
    Q4T_CHECK(policy.sha256 == Digest(kPolicy));
  }
  auto duplicate = valid;
  Field(duplicate, "targets")
      .array.push_back(valid.GetArray("targets")->array.front());
  Q4T_CHECK(!ParseMcpDeploymentPolicy(OutputJson(duplicate), &policy));
  Q4T_CHECK(!ParseMcpDeploymentPolicy(std::string(65537, ' '), &policy));
  Q4T_CHECK(!ParseMcpDeploymentPolicy(kPolicy, nullptr));
  return true;
}

Q4T_TEST(mcp_policy_scope_resolution_and_binding_survive_credential_rotation) {
  McpDeploymentPolicy policy;
  Q4T_CHECK(ParseMcpDeploymentPolicy(kPolicy, &policy));
  auto definition = Definition();
  Q4T_CHECK(
      ResolveMcpDeploymentTarget(&policy, "client:owner", &definition).ok());
  const auto binding = definition.target_binding;
  Q4T_CHECK(binding.size() == 64 &&
            definition.credentials.authorization == "user-token" &&
            definition.credentials.url == policy.targets[0].url);
  definition.credentials.authorization = "rotated-token";
  Q4T_CHECK(
      ResolveMcpDeploymentTarget(&policy, "client:owner", &definition).ok());
  Q4T_CHECK(definition.target_binding == binding);
  ++policy.fetch.timeout_ms;
  Q4T_CHECK(
      ResolveMcpDeploymentTarget(&policy, "client:owner", &definition).ok());
  Q4T_CHECK(definition.target_binding == binding);
  policy.targets[0].generation = "new-provider";
  Q4T_CHECK(
      ResolveMcpDeploymentTarget(&policy, "client:owner", &definition).ok());
  Q4T_CHECK(definition.target_binding != binding);
  Q4T_CHECK(
      ResolveMcpDeploymentTarget(&policy, "client:other", &definition).code ==
      "mcp_target_denied");
  Q4T_CHECK(definition.target_binding.empty() &&
            definition.credentials.url.empty());
  Q4T_CHECK(
      ResolveMcpDeploymentTarget(nullptr, "client:owner", &definition).code ==
      "mcp_target_mapping_required");
  auto tunnel = Definition("tunnel_id", "tunnel_project");
  Q4T_CHECK(ResolveMcpDeploymentTarget(&policy, "client:owner", &tunnel).ok());
  Q4T_CHECK(tunnel.credentials.url == policy.targets[1].url &&
            !tunnel.declaration.Has("server_url"));
  return true;
}

Q4T_TEST(mcp_policy_private_loading_rejects_links_fifo_and_permissions) {
  const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
  const auto parent = fs::absolute(output ? output : ".q4t-work/mcp-policy");
  fs::create_directories(parent);
  auto root = (parent / "policy-XXXXXX").string();
  Q4T_CHECK(mkdtemp(root.data()));
  struct Cleanup {
    std::string path;
    ~Cleanup() { fs::remove_all(path); }
  } cleanup{root};
  const auto path = fs::path(root) / "policy.json";
  { std::ofstream(path) << kPolicy; }
  Q4T_CHECK(chmod(path.c_str(), 0600) == 0);
  McpDeploymentPolicy policy;
  Q4T_CHECK(LoadMcpDeploymentPolicy(path.string(), &policy));
  fs::create_symlink(path, fs::path(root) / "link");
  Q4T_CHECK(!LoadMcpDeploymentPolicy(root + "/link", &policy));
  fs::create_hard_link(path, fs::path(root) / "hard");
  Q4T_CHECK(!LoadMcpDeploymentPolicy(path.string(), &policy));
  fs::remove(fs::path(root) / "hard");
  Q4T_CHECK(chmod(path.c_str(), 0644) == 0);
  Q4T_CHECK(!LoadMcpDeploymentPolicy(path.string(), &policy));
  Q4T_CHECK(mkfifo((root + "/fifo").c_str(), 0600) == 0);
  Q4T_CHECK(!LoadMcpDeploymentPolicy(root + "/fifo", &policy));
  Q4T_CHECK(!LoadMcpDeploymentPolicy(root, &policy));
  Q4T_CHECK(policy.sha256 == Digest(kPolicy));
  return true;
}

Q4T_TEST(mcp_policy_serve_option_is_optional_and_transactional) {
  ServerOptions options;
  options.model_dir = "/model-readonly";
  const std::vector<std::string_view> args{"--mcp-policy-file",
                                           "/private/mcp.json"};
  Q4T_CHECK(ParseServerOptions(args, &options));
  Q4T_CHECK(options.mcp_policy_file == "/private/mcp.json");
  const std::vector<std::string_view> missing{"--mcp-policy-file"};
  Q4T_CHECK(!ParseServerOptions(missing, &options));
  Q4T_CHECK(options.mcp_policy_file == "/private/mcp.json");
  options.mcp_policy_file = std::string("bad\0path", 8);
  Q4T_CHECK(!ValidateServerOptions(options));
  return true;
}

Q4T_TEST(
    mcp_policy_native_transport_has_distinct_authority_and_private_agents) {
  McpDeploymentPolicy policy;
  const auto raw = R"({"version":1,"targets":[{"kind":"tunnel_id",
    "id":"tunnel_project","transport":"native","generation":"generation-1",
    "scopes":["client:owner"],"agents":["client:private_agent"],"channel":"main"}]})";
  Q4T_CHECK(ParseMcpDeploymentPolicy(raw, &policy));
  const auto routes = McpDeploymentTunnelRoutes(&policy);
  Q4T_CHECK(routes.size() == 1 && routes[0].callers[0] == "client:owner" &&
            routes[0].agents[0] == "client:private_agent");
  auto definition = Definition("tunnel_id", "tunnel_project");
  Q4T_CHECK(
      ResolveMcpDeploymentTarget(&policy, "client:owner", &definition).ok());
  const auto native = definition.target_binding;
  Q4T_CHECK(definition.credentials.url.empty() &&
            definition.credentials.tunnel_id == "tunnel_project" &&
            definition.credentials.tunnel_generation == "generation-1");
  policy.targets[0].channel = "new-channel";
  Q4T_CHECK(
      ResolveMcpDeploymentTarget(&policy, "client:owner", &definition).ok());
  Q4T_CHECK(definition.target_binding != native);
  policy.targets[0].channel = "main";
  policy.targets[0].agents = {"client:rotated-agent"};
  Q4T_CHECK(
      ResolveMcpDeploymentTarget(&policy, "client:owner", &definition).ok());
  Q4T_CHECK(definition.target_binding == native);
  const auto public_config =
      OutputJson(McpDeploymentPolicyDescription(&policy));
  Q4T_CHECK(public_config.find("rotated-agent") == public_config.npos &&
            public_config.find("tunnel_project") == public_config.npos);
  Q4T_CHECK(ParseMcpDeploymentPolicy(kPolicy, &policy));
  Q4T_CHECK(
      ResolveMcpDeploymentTarget(&policy, "client:owner", &definition).ok());
  Q4T_CHECK(definition.credentials.tunnel_id.empty() &&
            definition.credentials.tunnel_generation.empty() &&
            definition.target_binding != native &&
            !definition.credentials.url.empty());
  Q4T_CHECK(McpDeploymentTunnelRoutes(&policy).empty());
  return true;
}

Q4T_TEST(mcp_policy_native_rejects_ambiguous_transport_scope_and_channels) {
  McpDeploymentPolicy policy;
  const auto raw = R"({"version":1,"targets":[{"kind":"tunnel_id",
    "id":"tunnel_project","transport":"native","generation":"generation-1",
    "scopes":["client:owner"],"agents":["client:private_agent"]}]})";
  Json original;
  Q4T_CHECK(Parse(raw, &original) && ParseMcpDeploymentPolicy(raw, &policy));
  for (auto patch :
       {Object({{"url", String("https://unwanted.example")}}),
        Object({{"agents", Array()}}),
        Object({{"agents", Array({String("*")})}}),
        Object({{"agents", Array({String("a"), String("a")})}}),
        Object({{"transport", Boolean(true)}}),
        Object({{"transport", String("other")}}), Object({{"channel", Json()}}),
        Object({{"id", String("path/ambiguous")}}),
        Object({{"kind", String("connector_id")},
                {"id", String("connector_googledrive")}})}) {
    auto changed = original;
    for (const auto& [key, value] : patch.object)
      Set(&Field(changed, "targets").array[0], key, value);
    Q4T_CHECK(!ParseMcpDeploymentPolicy(OutputJson(changed), &policy));
    Q4T_CHECK(policy.sha256 == Digest(raw));
  }
  return true;
}
