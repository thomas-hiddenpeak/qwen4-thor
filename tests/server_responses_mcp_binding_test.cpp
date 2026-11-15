#include "q4t/server/responses_mcp_binding.h"

#include "../src/server/object_protocol.h"
#include "q4t/server/responses_input.h"
#include "q4t/server/responses_compaction.h"
#include "q4t/server/responses_plan.h"
#include "q4t/server/responses_tool_search.h"
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

Q4T_TEST(responses_mcp_request_tools_preserve_original_schema_and_choices) {
  auto request = Object({{"input", Array({Catalog()})},
                         {"tools", Array({Definition().declaration})},
                         {"tool_choice", Value(R"({"type":"mcp",
                           "server_label":"project docs"})")}});
  Json tools, choice, effective;
  ChatPlanError error;
  Q4T_CHECK(
      NormalizeResponsesTools(request, &tools, &choice, &error, &effective));
  Q4T_CHECK(tools.array.size() == 1 && effective.array.size() == 1 &&
            effective.array[0].GetString("type") == "mcp");
  const auto& function = *tools.array[0].Find("function");
  Q4T_CHECK(function.GetString("name") ==
            ResponsesMcpModelName("project docs", "read project"));
  Q4T_CHECK(!function.GetBool("strict") &&
            OutputJson(*function.Find("parameters")) ==
                OutputJson(*Tool().Find("input_schema")));
  Q4T_CHECK(choice.Find("function")->GetString("name") ==
            function.GetString("name"));
  Set(&request, "input", Array());
  Q4T_CHECK(!NormalizeResponsesTools(request, &tools, &choice, &error));
  Q4T_CHECK(error.code == "mcp_catalog_resolution_required");
  return true;
}

Q4T_TEST(responses_mcp_discovery_uses_server_labels_and_distinct_active_slots) {
  auto first = Definition("first").declaration;
  auto second = Definition("second").declaration;
  Set(&first, "defer_loading", Boolean(true));
  Set(&second, "defer_loading", Boolean(true));
  const auto search = Value(R"({"type":"tool_search"})");
  const auto declarations = Array({first, second, search});
  ResponsesToolCatalog catalog;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesToolCatalog(declarations, &catalog, &error));
  Q4T_CHECK(catalog.eager.array.empty() && catalog.deferred.array.size() == 2 &&
            catalog.summaries.array[0].GetString("path") == "first");
  Json found;
  Q4T_CHECK(DiscoverResponsesTools(catalog, Value(R"({"paths":["second"]})"),
                                   &found, &error));
  Q4T_CHECK(found.array.size() == 1 &&
            found.array[0].GetString("server_label") == "second");
  auto history = Array({Catalog("first"), Catalog("second"),
                        Value(R"({"type":"message","role":"user",
                                    "content":"Read the docs"})")});
  Json tools, choice, active, header;
  auto request = Object({{"tools", declarations}, {"input", history}});
  Q4T_CHECK(NormalizeResponsesTools(request, &tools, &choice, &error, nullptr,
                                    &header, &active));
  Q4T_CHECK(tools.array.size() == 1);  // Only discovery before loading.
  for (const auto& tool : {first, second})
    history.array.push_back(Object({{"type", String("additional_tools")},
                                    {"role", String("developer")},
                                    {"tools", Array({tool})}}));
  Set(&request, "input", history);
  Q4T_CHECK(NormalizeResponsesTools(request, &tools, &choice, &error, nullptr,
                                    &header, &active));
  Q4T_CHECK(tools.array.size() == 3 && header.array.size() == 1 &&
            active.array.size() == 3);
  ResponsesInputPlan input;
  Q4T_CHECK(NormalizeResponsesInput(request, &input, &error));
  const auto messages = OutputJson(input.messages);
  Q4T_CHECK(messages.find(ResponsesMcpModelName("first", "read project")) !=
                messages.npos &&
            messages.find(ResponsesMcpModelName("second", "read project")) !=
                messages.npos);
  return true;
}

Q4T_TEST(responses_mcp_history_keeps_approval_failure_and_partial_semantics) {
  const auto history = Value(R"([
    {"type":"message","role":"user","content":"Read docs"},
    {"id":"mcpr_history","type":"mcp_approval_request",
     "name":"read project","server_label":"project docs",
     "arguments":"{\"path\":\"STATUS.md\"}"},
    {"type":"mcp_approval_response","approval_request_id":"mcpr_history",
     "approve":true},
    {"id":"mcp_history","type":"mcp_call","server_label":"project docs",
     "name":"read project","arguments":"{\"path\":\"STATUS.md\"}",
     "approval_request_id":"mcpr_history","status":"failed",
     "error":{"type":"http_error","code":503,"message":"Unavailable"}},
    {"id":"mcp_partial","type":"mcp_call","server_label":"project docs",
     "name":"read project","arguments":"{","status":"incomplete"}
  ])");
  ResponsesInputPlan input;
  ChatPlanError error;
  Q4T_CHECK(
      NormalizeResponsesInput(Object({{"input", history}}), &input, &error));
  Q4T_CHECK(input.items.array.size() == 5 && input.messages.array.size() == 5);
  Q4T_CHECK(input.messages.array[1].GetString("role") == "assistant" &&
            input.messages.array[2].GetString("role") == "user");
  const auto& calls = input.messages.array[3].GetArray("tool_calls")->array;
  Q4T_CHECK(calls.size() == 1 && calls[0].GetString("id") == "mcp_history");
  Q4T_CHECK(input.messages.array[3].GetString("content").find("no result") !=
            std::string::npos);
  const auto result = input.messages.array[4].GetString("content");
  Q4T_CHECK(result.find("Unavailable") != result.npos &&
            result.find("failed") != result.npos);
  return true;
}

Q4T_TEST(
    responses_mcp_planning_preserves_catalog_when_truncating_and_exhausted) {
  auto request = Object(
      {{"reasoning", Object({{"effort", String("none")}})},
       {"truncation", String("auto")},
       {"tools", Array({Definition().declaration})},
       {"input", Array({Catalog(), Value(R"({"type":"message","role":"user",
                                    "content":"old"})"),
                        Value(R"({"type":"message","role":"assistant",
                                    "content":"old answer"})"),
                        Value(R"({"type":"message","role":"user",
                                    "content":"current"})")})}});
  ChatPlanConfig config;
  config.model = "cpu";
  config.structured = true;
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan plan;
  ChatPlanError error;
  Q4T_CHECK(ParseResponsesEnvelope(request, config, &envelope, &error));
  Q4T_CHECK(PrepareResponsesGeneration(envelope, *request.Find("input"), config,
                                       &plan, &error));
  Q4T_CHECK(plan.generation_request.GetArray("tools")->array.size() == 1);
  ResponsesCompactionPlan compact;
  auto canonical = *request.Find("input");
  for (size_t i = 1; i < canonical.array.size(); ++i) {
    auto& item = canonical.array[i];
    Set(&item, "id", String("msg_mcp_context_" + std::to_string(i)));
    Set(&item, "status", String("completed"));
    const bool assistant = item.GetString("role") == "assistant";
    auto part =
        Object({{"type", String(assistant ? "output_text" : "input_text")},
                {"text", *item.Find("content")}});
    if (assistant) Set(&part, "annotations", Array());
    Set(&item, "content", Array({std::move(part)}));
  }
  Q4T_CHECK(PlanResponsesCompaction(canonical,
                                    ResponsesCompactionMode::kAutomatic, 128,
                                    config, &compact, &error));
  Q4T_CHECK(
      compact.compacted_items == 2 && compact.retained.array.size() == 2 &&
      compact.retained.array.front().GetString("type") == "mcp_list_tools");
  Q4T_CHECK(FitResponsesContext(
      envelope, config,
      [](const auto& candidate, ChatPlanError* failure) -> q4t::Status {
        if (candidate.input.messages.array.size() <= 1) return {};
        *failure = {"context_length_exceeded", "input", 400};
        return q4t::Status::Fail("fixture context");
      },
      &plan, &error));
  Q4T_CHECK(plan.truncated_items == 2 && plan.input.items.array.size() == 2 &&
            plan.input.items.array.front().GetString("type") ==
                "mcp_list_tools");
  envelope.native_tools_exhausted = true;
  const auto retained = plan.input.items;
  Q4T_CHECK(
      PrepareResponsesGeneration(envelope, retained, config, &plan, &error));
  Q4T_CHECK(plan.generation_request.GetArray("tools")->array.empty());
  return true;
}

Q4T_TEST(responses_mcp_request_declaration_only_never_grants_calls) {
  auto first = Definition().declaration;
  Set(&first, "authorization", String("request-only-secret"));
  auto request = Object({{"tools", Array({first})}});
  Json tools, choice, effective;
  ChatPlanError error;
  Q4T_CHECK(NormalizeResponsesCallableTools(request, &tools, &choice, &error,
                                            &effective, true));
  Q4T_CHECK(tools.array.empty() &&
            OutputJson(effective).find("request-only-secret") ==
                std::string::npos);
  Set(&request, "tool_choice",
      Object({{"type", String("mcp")},
              {"server_label", *first.Find("server_label")},
              {"name", String("not_known_until_listing")}}));
  Q4T_CHECK(NormalizeResponsesCallableTools(request, &tools, &choice, &error,
                                           &effective, true));
  Q4T_CHECK(tools.array.empty());
  Q4T_CHECK(!NormalizeResponsesCallableTools(request, &tools, &choice, &error,
                                            &effective));
  Q4T_CHECK(error.code == "mcp_catalog_resolution_required");
  Set(&request, "tools", Array({first, first}));
  Q4T_CHECK(!NormalizeResponsesCallableTools(request, &tools, &choice, &error,
                                             &effective, true));
  Q4T_CHECK(error.param == "tools[1].server_label");
  return true;
}

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
