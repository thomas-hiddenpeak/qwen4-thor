#include "q4t/server/responses_tool_search.h"

#include <stdexcept>
#include <cstdio>

#include "../src/server/responses_json.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::responses_detail;
Json Read(const char* text) {
  Json result;
  if (!q4t::io::ParseJson(text, &result)) throw std::runtime_error("Tool fixture");
  return result;
}
Json Tools() {
  return Read(R"([
    {"type":"tool_search"},
    {"type":"namespace","name":"project","description":"Project records",
     "tools":[
      {"type":"function","name":"ready","parameters":{"type":"object"}},
      {"type":"function","name":"read","defer_loading":true,"async":true,
       "parameters":{"type":"object","properties":{"private_schema_marker":
        {"type":"string"}},"required":["private_schema_marker"],
        "additionalProperties":false},"strict":true,
       "output_schema":{"type":"object","properties":{"title":
        {"type":"string"}},"required":["title"],"additionalProperties":false}},
      {"type":"custom","name":"lookup","defer_loading":true,
       "format":{"type":"text"}}]},
    {"type":"function","name":"status","description":"Service status",
     "defer_loading":true,"parameters":{"type":"object"}}
  ])");
}
ResponsesToolCatalog Catalog() {
  ResponsesToolCatalog result;
  ChatPlanError error;
  if (!PlanResponsesToolCatalog(Tools(), &result, &error))
    throw std::runtime_error("Tool catalog fixture: " + error.param);
  return result;
}
}  // namespace

Q4T_TEST(responses_tool_catalog_hides_deferred_schemas_and_namespace_children) {
  auto tools = Tools();
  const auto original = OutputJson(tools);
  ResponsesToolCatalog plan;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesToolCatalog(tools, &plan, &error));
  Q4T_CHECK(OutputJson(tools) == original &&
            plan.search.GetString("execution") == "server" &&
            plan.eager.array.size() == 1 && plan.deferred.array.size() == 2 &&
            plan.summaries.array.size() == 2);
  const auto& children = plan.eager.array[0].GetArray("tools")->array;
  Q4T_CHECK(children.size() == 1 && children[0].GetString("name") == "ready");
  const auto summary = OutputJson(plan.summaries);
  Q4T_CHECK(summary.find("private_schema_marker") == summary.npos &&
            summary.find("lookup") == summary.npos &&
            summary.find("output_schema") == summary.npos &&
            summary.find("Project records") != summary.npos);
  return true;
}

Q4T_TEST(responses_tool_search_loads_exact_authorized_subset_and_no_duplicates) {
  const auto plan = Catalog();
  Json loaded;
  ChatPlanError error;
  Q4T_CHECK(DiscoverResponsesTools(
      plan, Read(R"({"paths":["project.read","status","status","unknown"]})"),
      &loaded, &error));
  Q4T_CHECK(loaded.array.size() == 2 &&
            loaded.array[0].GetArray("tools")->array.size() == 1);
  const auto& definition = loaded.array[0].GetArray("tools")->array[0];
  Q4T_CHECK(OutputJson(definition) ==
            OutputJson(Tools().array[1].GetArray("tools")->array[1]));
  Q4T_CHECK(definition.GetBool("defer_loading") && definition.GetBool("async") &&
            definition.Find("output_schema"));
  Q4T_CHECK(DiscoverResponsesTools(plan, Read(R"({"paths":["project"]})"),
                                    &loaded, &error));
  Q4T_CHECK(loaded.array.size() == 1 &&
            loaded.array[0].GetArray("tools")->array.size() == 2);
  Q4T_CHECK(DiscoverResponsesTools(plan, Read(R"({"paths":["missing"]})"),
                                    &loaded, &error) && loaded.array.empty());
  return true;
}

Q4T_TEST(responses_tool_search_client_catalog_preserves_schema_without_execution) {
  auto tools = Tools();
  tools.array[0] = Read(R"({"type":"tool_search","execution":"client",
    "description":"Find project tools","parameters":{"type":"object",
    "properties":{"goal":{"type":"string","minLength":1}},
    "required":["goal"],"additionalProperties":false}})");
  ResponsesToolCatalog plan;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesToolCatalog(tools, &plan, &error) &&
            OutputJson(plan.search) == OutputJson(tools.array[0]));
  Json loaded;
  Q4T_CHECK(!DiscoverResponsesTools(plan, Read(R"({"paths":["project"]})"),
                                     &loaded, &error) && loaded.array.empty());
  return true;
}

Q4T_TEST(responses_tool_catalog_rejects_bad_definitions_before_publishing) {
  for (const auto* text : {
      R"([{"type":"tool_search","execution":null}])",
      R"([{"type":"tool_search","execution":"automatic"}])",
      R"([{"type":"tool_search"},{"type":"tool_search"}])",
      R"([{"type":"tool_search","description":true}])",
      R"([{"type":"tool_search","parameters":{"type":"broken"}}])",
      R"([{"type":"tool_search"},{"type":"function","name":"read","defer_loading":null}])",
      R"([{"type":"tool_search"},{"type":"function","name":"read","defer_loading":true,"parameters":{"type":"broken"}}])",
      R"([{"type":"tool_search"},{"type":"function","name":"read","defer_loading":true,"output_schema":{"type":"broken"}}])",
      R"([{"type":"namespace","name":"bad","description":"bad","tools":[{"type":"function","name":"same","defer_loading":true},{"type":"function","name":"same"}]}])"}) {
    auto plan = Catalog();
    ChatPlanError error;
    const auto status = PlanResponsesToolCatalog(Read(text), &plan, &error);
    if (status || error.param.empty())
      std::fprintf(stderr, "catalog case %s: %s (%s)\n", text,
                   error.param.c_str(), status.message().c_str());
    Q4T_CHECK(!status &&
              plan.eager.IsNull() && plan.deferred.IsNull() &&
              plan.search.IsNull() && !error.param.empty());
  }
  auto tools = Tools();
  Set(&tools.array[2], "description",
      String(std::string(kResponsesToolCatalogBytes, 'x')));
  ResponsesToolCatalog plan;
  ChatPlanError error;
  Q4T_CHECK(!PlanResponsesToolCatalog(tools, &plan, &error) &&
            error.http_status == 503 && plan.eager.IsNull());
  return true;
}

Q4T_TEST(responses_tool_search_rejects_bad_queries_without_partial_loading) {
  const auto plan = Catalog();
  for (const auto* text : {R"({})", R"({"paths":[]})",
                           R"({"paths":["project",1]})",
                           R"({"paths":["project"],"owner":"other"})"}) {
    Json loaded = plan.deferred;
    ChatPlanError error;
    Q4T_CHECK(!DiscoverResponsesTools(plan, Read(text), &loaded, &error) &&
              loaded.IsArray() && loaded.array.empty());
  }
  auto paths = Array();
  for (size_t i = 0; i <= kResponsesToolSearchPaths; ++i)
    paths.array.push_back(String("project"));
  Json loaded;
  ChatPlanError error;
  Q4T_CHECK(!DiscoverResponsesTools(plan, Object({{"paths", paths}}),
                                     &loaded, &error) &&
            error.http_status == 503 && loaded.array.empty());
  return true;
}

Q4T_TEST(responses_tool_history_loads_client_definitions_outside_initial_catalog) {
  const auto history = Read(R"([
    {"type":"tool_search_call","execution":"client","call_id":"call_search",
     "arguments":{"goal":"Read documentation"}},
    {"type":"tool_search_output","call_id":"call_search","tools":[
     {"type":"function","name":"read_document","defer_loading":true,
      "parameters":{"type":"object"},"output_schema":{"type":"string"}}]},
    {"type":"message","role":"user","content":"Use the loaded tool"}
  ])");
  const auto before = OutputJson(history);
  ResponsesToolHistory plan;
  ChatPlanError error;
  const auto status = ResolveResponsesToolHistory(Array(), history, &plan, &error);
  if (!status)
    std::fprintf(stderr, "client history: %s (%s)\n", error.param.c_str(),
                 status.message().c_str());
  Q4T_CHECK(status);
  Q4T_CHECK(plan.active.array.size() == 1 && plan.activations.size() == 1 &&
            plan.activations[0].item_index == 1 &&
            plan.active.array[0].GetString("name") == "read_document" &&
            plan.active.array[0].GetBool("defer_loading") &&
            OutputJson(history) == before);
  return true;
}

Q4T_TEST(responses_tool_history_additions_preserve_order_and_previous_schemas) {
  const auto history = Read(R"([
    {"type":"additional_tools","role":"developer","tools":[
      {"type":"namespace","name":"project","description":"Project docs",
       "tools":[{"type":"function","name":"read","output_schema":
        {"type":"string","enum":["original"]}}]}]},
    {"type":"function_call","name":"read","namespace":"project",
     "call_id":"original_call","arguments":"{}"},
    {"type":"additional_tools","role":"developer","tools":[
      {"type":"namespace","name":"project","description":"New project docs",
       "tools":[{"type":"function","name":"read","output_schema":
        {"type":"string","enum":["updated"]}},
        {"type":"custom","name":"lookup","format":{"type":"text"}}]}]}
  ])");
  ResponsesToolHistory plan;
  ChatPlanError error;
  Q4T_CHECK(ResolveResponsesToolHistory(Array(), history, &plan, &error));
  Q4T_CHECK(plan.activations.size() == 2 &&
            plan.activations[0].item_index == 0 &&
            plan.activations[1].item_index == 2);
  Q4T_CHECK(OutputJson(plan.activations[0].definitions).find("original") !=
            std::string::npos);
  const auto active = OutputJson(plan.active);
  Q4T_CHECK(active.find("original") == active.npos &&
            active.find("updated") != active.npos &&
            plan.active.array[0].GetArray("tools")->array.size() == 2);
  return true;
}

Q4T_TEST(responses_tool_history_matches_search_results_and_never_replays) {
  for (const auto* text : {
      R"([{"type":"tool_search_output","call_id":"missing","tools":[]}])",
      R"([{"type":"tool_search_call","call_id":"pending","arguments":{}}])",
      R"([{"type":"tool_search_call","execution":"server","call_id":"","arguments":{}}])",
      R"([{"type":"tool_search_call","call_id":"c","arguments":{}},{"type":"tool_search_output","execution":"server","tools":[]}])",
      R"([{"type":"tool_search_call","call_id":"c","arguments":{}},{"type":"tool_search_output","call_id":"c","tools":[]},{"type":"tool_search_output","call_id":"c","tools":[]}])",
      R"([{"type":"tool_search_call","call_id":"c","arguments":{}},{"type":"tool_search_output","call_id":"c","status":"in_progress","tools":[]}])",
      R"([{"type":"additional_tools","role":"user","tools":[]}])"}) {
    ResponsesToolHistory plan;
    ChatPlanError error;
    Q4T_CHECK(!ResolveResponsesToolHistory(Array(), Read(text), &plan, &error) &&
              plan.active.IsNull() && plan.activations.empty());
  }
  return true;
}

Q4T_TEST(responses_tool_history_incomplete_results_do_not_authorize_tools) {
  const auto history = Read(R"([
    {"type":"tool_search_call","execution":"server","arguments":{"paths":["project"]}},
    {"type":"tool_search_output","execution":"server","status":"incomplete",
     "tools":[{"type":"function","name":"should_not_load"}]}
  ])");
  ResponsesToolHistory plan;
  ChatPlanError error;
  const auto status = ResolveResponsesToolHistory(Array(), history, &plan, &error);
  if (!status)
    std::fprintf(stderr, "incomplete history: %s (%s)\n", error.param.c_str(),
                 status.message().c_str());
  Q4T_CHECK(status &&
            plan.active.array.empty() && plan.activations.empty());
  return true;
}

Q4T_TEST(responses_tool_history_catalogs_are_request_local_and_bounded) {
  const auto first = Read(R"([
    {"type":"additional_tools","role":"developer","tools":[
      {"type":"function","name":"tenant_a_only","defer_loading":true}]}
  ])");
  ResponsesToolHistory a, b;
  ChatPlanError error;
  const auto first_status = ResolveResponsesToolHistory(Array(), first, &a, &error);
  if (!first_status)
    std::fprintf(stderr, "first history: %s (%s)\n", error.param.c_str(),
                 first_status.message().c_str());
  const auto second_status = ResolveResponsesToolHistory(Array(), Array(), &b, &error);
  if (!second_status)
    std::fprintf(stderr, "empty history: %s (%s)\n", error.param.c_str(),
                 second_status.message().c_str());
  Q4T_CHECK(first_status && second_status &&
            a.active.array.size() == 1 && b.active.array.empty());
  auto oversized = Array();
  for (size_t i = 0; i < 129; ++i) oversized.array.push_back(first.array[0]);
  Q4T_CHECK(!ResolveResponsesToolHistory(Array(), oversized, &a, &error) &&
            error.http_status == 503 && a.active.IsNull());
  auto invalid = first;
  Set(&Field(invalid.array[0], "tools").array[0], "output_schema",
      Read(R"({"type":"invalid"})"));
  Q4T_CHECK(!ResolveResponsesToolHistory(Array(), invalid, &a, &error) &&
            error.param.starts_with("input[0].tools[0].output_schema") &&
            a.active.IsNull());
  return true;
}
