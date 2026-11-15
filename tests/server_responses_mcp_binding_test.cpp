#include "q4t/server/responses_mcp_binding.h"

#include "../src/server/object_protocol.h"
#include "q4t/server/responses_input.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
Json Value(const std::string& text) {
  Json result;
  if (!Parse(text, &result)) throw std::runtime_error("MCP binding fixture");
  return result;
}
ResponsesMcpDefinition Definition(std::string label = "project docs",
                                  Json patch = Object()) {
  auto declaration = Object(
      {{"type", String("mcp")},
       {"server_label", String(std::move(label))},
       {"server_url", String("https://tools.example/mcp/private-endpoint")},
       {"authorization", String("private-token")},
       {"headers", Object({{"X-Key", String("private-key")}})}});
  for (const auto& [key, value] : patch.object) Set(&declaration, key, value);
  ResponsesMcpDefinition result;
  ChatPlanError error;
  if (!PlanResponsesMcpDefinition(declaration, &result, &error))
    throw std::runtime_error("MCP definition fixture");
  return result;
}
Json Tool(std::string name = "read project") {
  return Object({{"name", String(std::move(name))},
                 {"description", String("Read the original project document")},
                 {"input_schema", Value(R"({"type":"object","properties":{
      "path":{"type":"string"},"limit":{"type":"integer","minimum":1}},
      "required":["path"],"additionalProperties":false})")},
                 {"annotations", Object({{"readOnlyHint", Boolean(true)}})}});
}
Json Catalog(std::string label = "project docs", Json tools = Array({Tool()})) {
  return Object({{"type", String("mcp_list_tools")},
                 {"id", String("mcpl_" + Digest(label).substr(0, 32))},
                 {"server_label", String(std::move(label))},
                 {"tools", std::move(tools)}});
}
}  // namespace

Q4T_TEST(responses_mcp_bindings_preserve_schema_and_distinguish_servers) {
  std::vector<ResponsesMcpBinding> bindings;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpBindings({Definition(), Definition("资料服务")},
                                     Array({Catalog(), Catalog("资料服务")}),
                                     &bindings, &error));
  Q4T_CHECK(bindings.size() == 2 && bindings[0].name == bindings[1].name &&
            bindings[0].model_name != bindings[1].model_name);
  auto definitions = Array();
  for (const auto& binding : bindings) {
    Q4T_CHECK(OutputJson(*binding.function.Find("parameters")) ==
              OutputJson(*Tool().Find("input_schema")));
    Q4T_CHECK(!binding.function.GetBool("strict") && binding.direct &&
              !binding.programmatic && binding.approval &&
              !binding.catalog_id.empty());
    const auto model = OutputJson(binding.function);
    Q4T_CHECK(model.find("private-") == model.npos &&
              model.find("tools.example") == model.npos);
    definitions.array.push_back(binding.function);
  }
  Json normalized, choice;
  Q4T_CHECK(NormalizeResponsesCallableTools(Object({{"tools", definitions}}),
                                            &normalized, &choice, &error));
  Q4T_CHECK(normalized.array.size() == 2);
  for (size_t i = 0; i < 2; ++i) {
    Q4T_CHECK(normalized.array[i].Find("function")->GetString("name") ==
              bindings[i].model_name);
    Q4T_CHECK(
        OutputJson(*normalized.array[i].Find("function")->Find("parameters")) ==
        OutputJson(*Tool().Find("input_schema")));
  }
  return true;
}

Q4T_TEST(responses_mcp_bindings_filter_callers_and_deferred_metadata) {
  const auto definition = Definition("project docs", Value(R"({
    "allowed_tools":{"read_only":true},"require_approval":"never",
    "allowed_callers":["programmatic"],"defer_loading":true})"));
  auto write = Tool("write");
  Set(&write, "annotations", Json());
  std::vector<ResponsesMcpBinding> bindings;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpBindings(
      {definition}, Array({Catalog("project docs", Array({Tool(), write}))}),
      &bindings, &error));
  Q4T_CHECK(bindings.size() == 1 && !bindings[0].direct &&
            bindings[0].programmatic && !bindings[0].approval &&
            bindings[0].function.GetBool("defer_loading"));
  Json mapped;
  Q4T_CHECK(!MapResponsesMcpChoice(Value(R"({"type":"mcp",
    "server_label":"project docs"})"),
                                   bindings, &mapped, &error));
  Q4T_CHECK(error.http_status == 400 && mapped.IsNull());
  return true;
}

Q4T_TEST(responses_mcp_binding_choice_named_server_and_allowed_set) {
  std::vector<ResponsesMcpBinding> bindings;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpBindings(
      {Definition()},
      Array({Catalog("project docs", Array({Tool(), Tool("second")}))}),
      &bindings, &error));
  Json mapped;
  Q4T_CHECK(MapResponsesMcpChoice(Value(R"({"type":"mcp",
    "server_label":"project docs","name":"second"})"),
                                  bindings, &mapped, &error));
  Q4T_CHECK(mapped.GetString("type") == "function" &&
            mapped.GetString("name") == bindings[1].model_name);
  Q4T_CHECK(MapResponsesMcpChoice(Value(R"({"type":"mcp",
    "server_label":"project docs","name":null})"),
                                  bindings, &mapped, &error));
  Q4T_CHECK(mapped.GetString("type") == "allowed_tools" &&
            mapped.GetString("mode") == "required" &&
            mapped.GetArray("tools")->array.size() == 2);
  Q4T_CHECK(
      MapResponsesMcpChoice(Value(R"({"type":"allowed_tools","mode":"auto",
    "tools":[{"type":"mcp","server_label":"project docs"},
             {"type":"function","name":"local"}]})"),
                            bindings, &mapped, &error));
  Q4T_CHECK(mapped.GetString("mode") == "auto" &&
            mapped.GetArray("tools")->array.size() == 3 &&
            mapped.GetArray("tools")->array.back().GetString("name") ==
                "local");
  for (
      const auto* selector :
      {R"({"type":"mcp","server_label":"absent"})",
       R"({"type":"mcp","server_label":"project docs","name":"absent"})",
       R"({"type":"mcp","server_label":"project docs","name":1})",
       R"({"type":"mcp","server_label":"project docs","authorization":"bad"})"})
    Q4T_CHECK(
        !MapResponsesMcpChoice(Value(selector), bindings, &mapped, &error));
  return true;
}

Q4T_TEST(responses_mcp_bindings_require_unambiguous_validated_catalogs) {
  std::vector<ResponsesMcpBinding> bindings;
  ChatPlanError error;
  Q4T_CHECK(
      !PlanResponsesMcpBindings({Definition()}, Array(), &bindings, &error));
  Q4T_CHECK(error.code == "mcp_catalog_resolution_required");
  Q4T_CHECK(!PlanResponsesMcpBindings(
      {Definition()}, Array({Catalog(), Catalog()}), &bindings, &error));
  Q4T_CHECK(!PlanResponsesMcpBindings({Definition(), Definition()},
                                      Array({Catalog()}), &bindings, &error));
  auto invalid = Tool();
  Set(&invalid, "input_schema",
      Value(R"({"type":"object","properties":{"x":3}})"));
  Q4T_CHECK(!PlanResponsesMcpBindings(
      {Definition()}, Array({Catalog("project docs", Array({invalid}))}),
      &bindings, &error));
  Q4T_CHECK(bindings.empty() && error.code == "invalid_mcp_schema");
  auto failed = Catalog("project docs", Array());
  Set(&failed, "error", String("unavailable"));
  Q4T_CHECK(PlanResponsesMcpBindings({Definition()}, Array({failed}), &bindings,
                                     &error));
  Q4T_CHECK(bindings.empty());
  Json mapped;
  Q4T_CHECK(!MapResponsesMcpChoice(Value(R"({"type":"mcp",
    "server_label":"project docs"})"),
                                   bindings, &mapped, &error));
  return true;
}

Q4T_TEST(responses_mcp_binding_budgets_reject_expansion_without_truncation) {
  auto tools = Array();
  for (size_t i = 0; i < 129; ++i)
    tools.array.push_back(Tool("read_" + std::to_string(i)));
  std::vector<ResponsesMcpBinding> bindings;
  ChatPlanError error;
  Q4T_CHECK(!PlanResponsesMcpBindings({Definition()},
                                      Array({Catalog("project docs", tools)}),
                                      &bindings, &error));
  Q4T_CHECK(error.http_status == 503 && bindings.empty());
  tools.array.pop_back();
  Q4T_CHECK(PlanResponsesMcpBindings({Definition()},
                                     Array({Catalog("project docs", tools)}),
                                     &bindings, &error));
  Q4T_CHECK(bindings.size() == 128);
  Json mapped;
  Q4T_CHECK(
      !MapResponsesMcpChoice(Value(R"({"type":"allowed_tools","mode":"auto",
    "tools":[{"type":"mcp","server_label":"project docs"},
             {"type":"function","name":"local"}]})"),
                             bindings, &mapped, &error));
  Q4T_CHECK(error.http_status == 503 && mapped.IsNull());
  return true;
}
