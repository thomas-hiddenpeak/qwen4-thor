#include "q4t/server/responses_file_search.h"

#include <cmath>
#include <limits>

#include "../src/server/object_protocol.h"
#include "q4t/test.h"
#include "q4t/server/responses_input.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
Json Read(const std::string& text) {
  Json result;
  if (!Parse(text, &result)) throw std::runtime_error("Tool contract fixture");
  return result;
}
Json Definition() {
  return Object(
      {{"type", String("file_search")},
       {"vector_store_ids", Array({String("vs_docs"), String("vs_runs")})}});
}
ResponsesFileSearchTool Tool() {
  ResponsesFileSearchTool tool;
  ChatPlanError error;
  if (!PlanResponsesFileSearchTool(Definition(), &tool, &error))
    throw std::runtime_error("Tool fixture planning");
  return tool;
}
std::vector<VectorSearchChunk> Chunks() {
  return {{"file_a", "model.md", "a", "apple orchard", Object(), {1, 0}},
          {"file_b", "runs.md", "b", "quantum physics", Object(), {0, 1}}};
}
std::vector<VectorSearchQuery> Queries() { return {{"quantum", {1, 0}}}; }
VectorSearchPlan Search(double embedding, double text) {
  auto plan = Tool().search;
  plan.queries = {"quantum"};
  plan.embedding_weight = embedding;
  plan.text_weight = text;
  return plan;
}
}  // namespace

Q4T_TEST(responses_file_search_definition_preserves_controls_and_owner_ids) {
  auto request = Definition();
  Set(&request, "filters", Read(R"({"type":"and","filters":[
    {"key":"revision","value":9007199254740993},
    {"type":"in","key":"kind","value":["run","release"]}]})"));
  Set(&request, "max_num_results", Number(7));
  Set(&request, "ranking_options", Read(R"({"ranker":"default-2024-11-15",
    "score_threshold":0.2,"hybrid_search":{"embedding_weight":2,
    "text_weight":1}})"));
  ResponsesFileSearchTool plan;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesFileSearchTool(request, &plan, &error, "tools[2]") &&
            plan.store_ids ==
                std::vector<std::string>({"vs_docs", "vs_runs"}) &&
            plan.search.queries.empty() && plan.search.max_results == 7 &&
            plan.search.ranker == "default-2024-11-15" &&
            plan.search.score_threshold == 0.2 &&
            plan.search.embedding_weight == 2 && plan.search.text_weight == 1 &&
            OutputJson(plan.search.filters).find("9007199254740993") !=
                std::string::npos);
  const auto defaults = Tool();
  Q4T_CHECK(defaults.search.max_results == 10 &&
            defaults.search.embedding_weight == 1 &&
            defaults.search.text_weight == 1);
  return true;
}

Q4T_TEST(responses_file_search_bad_definition_never_publishes_partial_plan) {
  for (
      const auto* json :
      {R"({"type":"file_search","vector_store_ids":[]})",
       R"({"type":"file_search","vector_store_ids":["vs_a","vs_a"]})",
       R"({"type":"file_search","vector_store_ids":["vs_../path"]})",
       R"({"type":"file_search","vector_store_ids":["file_a"]})",
       R"({"type":"file_search","vector_store_ids":["vs_a"],"owner":"other"})",
       R"({"type":"file_search","vector_store_ids":["vs_a"],"ranking_options":{"ranker":"none"}})",
       R"({"type":"file_search","vector_store_ids":["vs_a"],"ranking_options":null})",
       R"({"type":"file_search","vector_store_ids":["vs_a"],"max_num_results":0})"}) {
    auto plan = Tool();
    ChatPlanError error;
    Q4T_CHECK(
        !PlanResponsesFileSearchTool(Read(json), &plan, &error, "tools[3]") &&
        plan.store_ids.empty() && error.param.starts_with("tools[3]"));
  }
  auto request = Definition();
  auto ids = Array();
  for (size_t i = 0; i <= kResponsesSearchStores; ++i)
    ids.array.push_back(String("vs_" + std::to_string(i)));
  Set(&request, "vector_store_ids", ids);
  ResponsesFileSearchTool plan;
  ChatPlanError error;
  Q4T_CHECK(!PlanResponsesFileSearchTool(request, &plan, &error) &&
            error.http_status == 503 && plan.store_ids.empty());
  return true;
}

Q4T_TEST(responses_file_search_weights_validate_exact_sign_and_precision) {
  for (const auto* raw :
       {R"({"embedding_weight":-1e-500,"text_weight":1})",
        R"({"embedding_weight":0,"text_weight":0})",
        R"({"embedding_weight":1})",
        R"({"embedding_weight":1,"text_weight":true})",
        R"({"embedding_weight":1,"text_weight":1,"extra":1})", "null"}) {
    auto request = Definition();
    Set(&request, "ranking_options", Object({{"hybrid_search", Read(raw)}}));
    ResponsesFileSearchTool plan;
    ChatPlanError error;
    Q4T_CHECK(!PlanResponsesFileSearchTool(request, &plan, &error) &&
              plan.store_ids.empty());
  }
  auto request = Definition();
  Set(&request, "ranking_options", Read(R"({"hybrid_search":{
    "embedding_weight":1e-500,"text_weight":1}})"));
  ResponsesFileSearchTool plan;
  ChatPlanError error;
  Q4T_CHECK(!PlanResponsesFileSearchTool(request, &plan, &error) &&
            error.code == "vector_search_resource_exhausted");
  return true;
}

Q4T_TEST(responses_file_search_call_cannot_override_trusted_tool_controls) {
  auto tool = Tool();
  tool.search.filters = Read(R"({"key":"visibility","value":"project"})");
  tool.search.max_results = 2;
  tool.search.embedding_weight = 3;
  VectorSearchPlan plan;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesFileSearchCall(
      tool, Read(R"({"queries":["PLE SSD","MTP"]})"), &plan, &error));
  Q4T_CHECK(plan.queries == std::vector<std::string>({"PLE SSD", "MTP"}) &&
            plan.max_results == 2 && plan.embedding_weight == 3 &&
            OutputJson(plan.filters) == OutputJson(tool.search.filters));
  for (const auto* raw :
       {R"({"queries":["x"],"vector_store_ids":["vs_other"]})",
        R"({"queries":["x"],"filters":null})",
        R"({"queries":["x"],"max_num_results":50})", R"({"queries":"x"})",
        R"({"queries":[]})", R"({"queries":[""]})"})
    Q4T_CHECK(!PlanResponsesFileSearchCall(tool, Read(raw), &plan, &error) &&
              plan.queries.empty());
  return true;
}

Q4T_TEST(responses_file_search_native_call_schema_uses_real_chat_contract) {
  ChatPlanConfig config;
  config.model = "native-qwen";
  config.structured = true;
  const auto tool = NativeFileSearchDefinition("q4t_file_search_0");
  const auto request =
      Object({{"messages",
               Array({Object({{"role", String("user")},
                              {"content", String("Find project data")}})})},
              {"tools", Array({tool})},
              {"tool_choice",
               Object({{"type", String("function")},
                       {"function",
                        Object({{"name", String("q4t_file_search_0")}})}})}});
  ChatRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(PlanChatRequest(request, config, &plan, &error));
  const auto* parameters = tool.Find("function")->Find("parameters");
  Q4T_CHECK(parameters->Find("properties")->object.size() == 1 &&
            parameters->Find("properties")->Has("queries") &&
            !parameters->GetBool("additionalProperties"));
  return true;
}

Q4T_TEST(responses_file_search_weights_control_real_semantic_and_text_ranks) {
  ChatPlanError error;
  std::vector<VectorSearchHit> hits;
  for (const auto& [semantic, text] :
       {std::pair{1.0, 0.0}, {0.0, 1.0}, {3.0, 1.0}, {1.0, 3.0}}) {
    const auto plan = Search(semantic, text);
    Q4T_CHECK(RankVectorSearch(plan, Queries(), Chunks(), {}, &hits, &error) &&
              !hits.empty() && hits[0].chunk == (semantic > text ? 0 : 1));
    if (!semantic || !text)
      Q4T_CHECK(hits.size() == 1 && hits[0].score == 1);
    else
      Q4T_CHECK(hits.size() == 2 && hits[0].score == 0.75 &&
                hits[1].score == 0.25);
  }
  return true;
}

Q4T_TEST(responses_file_search_weight_scaling_cannot_overflow_scores) {
  ChatPlanError error;
  std::vector<VectorSearchHit> ordinary, huge;
  Q4T_CHECK(RankVectorSearch(Search(1, 1), Queries(), Chunks(), {}, &ordinary,
                             &error) &&
            RankVectorSearch(Search(1e308, 1e308), Queries(), Chunks(), {},
                             &huge, &error));
  Q4T_CHECK(ordinary.size() == huge.size());
  for (size_t i = 0; i < ordinary.size(); ++i)
    Q4T_CHECK(ordinary[i].chunk == huge[i].chunk &&
              ordinary[i].score == huge[i].score &&
              std::isfinite(huge[i].score));
  Q4T_CHECK(RankVectorSearch(Search(1e308, 5e307), Queries(), Chunks(), {},
                             &huge, &error) &&
            huge[0].chunk == 0 && std::abs(huge[0].score - 2.0 / 3.0) < 1e-15);
  return true;
}

Q4T_TEST(responses_file_search_invalid_runtime_weights_never_publish_hits) {
  ChatPlanError error;
  for (const auto value : {-1.0, std::numeric_limits<double>::infinity(),
                           std::numeric_limits<double>::quiet_NaN()}) {
    std::vector<VectorSearchHit> hits{{0, 1}};
    Q4T_CHECK(!RankVectorSearch(Search(value, 1), Queries(), Chunks(), {},
                                &hits, &error) &&
              hits.empty());
  }
  std::vector<VectorSearchHit> hits;
  Q4T_CHECK(
      !RankVectorSearch(Search(0, 0), Queries(), Chunks(), {}, &hits, &error) &&
      hits.empty());
  return true;
}

Q4T_TEST(responses_file_search_private_names_preserve_public_tools) {
  auto request = Object({{"tools", Array({Definition(), Read(R"({
    "type":"function","name":"q4t_file_search_0",
    "parameters":{"type":"object","properties":{},
    "additionalProperties":false}})")})}});
  std::vector<ResponsesFileSearchBinding> bindings;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesFileSearchBindings(request, &bindings, &error) &&
            bindings.size() == 1 && bindings[0].tool_index == 0 &&
            bindings[0].model_name == "q4t_file_search_0_0");
  Json tools, choice, effective;
  Q4T_CHECK(
      NormalizeResponsesTools(request, &tools, &choice, &error, &effective));
  Q4T_CHECK(tools.array[0].Find("function")->GetString("name") ==
                bindings[0].model_name &&
            OutputJson(effective.array[0]) == OutputJson(Definition()) &&
            tools.array[1].Find("function")->GetString("name") ==
                "q4t_file_search_0");
  return true;
}

Q4T_TEST(responses_file_search_forced_and_allowed_choices_map_all_bindings) {
  auto request = Object({{"tools", Array({Definition(), Definition()})},
                         {"tool_choice", Read(R"({"type":"file_search"})")}});
  Json tools, choice;
  ChatPlanError error;
  Q4T_CHECK(NormalizeResponsesTools(request, &tools, &choice, &error) &&
            choice.GetString("type") == "allowed_tools");
  const auto* allowed = choice.Find("allowed_tools");
  Q4T_CHECK(allowed->GetString("mode") == "required" &&
            allowed->GetArray("tools")->array.size() == 2);
  Set(&request, "tool_choice", Read(R"({"type":"allowed_tools","mode":"auto",
    "tools":[{"type":"file_search"}]})"));
  Q4T_CHECK(NormalizeResponsesTools(request, &tools, &choice, &error) &&
            choice.Find("allowed_tools")->GetString("mode") == "auto");
  for (const auto* bad : {R"({"type":"file_search","name":"private"})",
                          R"({"type":"allowed_tools","mode":"auto",
                          "tools":[{"type":"file_search","filters":null}]})"}) {
    Set(&request, "tool_choice", Read(bad));
    Q4T_CHECK(!NormalizeResponsesTools(request, &tools, &choice, &error) &&
              error.param.starts_with("tool_choice"));
  }
  Set(&request, "tools", Array());
  Set(&request, "tool_choice", Read(R"({"type":"file_search"})"));
  Q4T_CHECK(!NormalizeResponsesTools(request, &tools, &choice, &error));
  return true;
}

Q4T_TEST(responses_file_search_result_adapter_never_fabricates_missing_data) {
  auto page = Read(R"({"object":"vector_store.search_results.page",
    "has_more":false,"data":[{"file_id":"file_actual","filename":"STATUS.md",
      "attributes":{"release":80},"score":0.5,
      "content":[{"type":"text","text":"实际源文档"}]}]})");
  Json results;
  Q4T_CHECK(ResponsesFileSearchResults(page, &results) &&
            results.array.size() == 1 &&
            results.array[0].GetString("text") == "实际源文档" &&
            results.array[0].GetNumber("score") == 0.5);
  for (const auto* field : {"file_id", "filename", "score", "content"}) {
    auto bad = page;
    Set(&Field(bad, "data").array[0], field, Json());
    Q4T_CHECK(!ResponsesFileSearchResults(bad, &results) && results.IsNull());
  }
  Set(&page, "has_more", Boolean(true));
  Q4T_CHECK(!ResponsesFileSearchResults(page, &results) && results.IsNull());
  return true;
}

Q4T_TEST(
    responses_file_search_history_retains_results_with_parallel_functions) {
  for (const bool native_first : {false, true}) {
    const auto file = Read(R"({"type":"file_search_call","id":"fs_old",
      "status":"completed","queries":["PLE"],"results":[
      {"file_id":"file_real","filename":"MODEL.md","score":0.5,
       "text":"PLE from SSD"}]})");
    const auto fn = Read(R"({"type":"function_call","call_id":"call_ext",
      "name":"lookup","arguments":"{}"})");
    auto input = Array();
    input.array.push_back(native_first ? file : fn);
    input.array.push_back(native_first ? fn : file);
    input.array.push_back(Read(R"({"type":"function_call_output",
      "call_id":"call_ext","output":"client result"})"));
    ResponsesInputPlan plan;
    ChatPlanError error;
    Q4T_CHECK(
        NormalizeResponsesInput(Object({{"input", input}}), &plan, &error));
    Q4T_CHECK(plan.messages.array.size() == 3 &&
              plan.messages.array[0].GetArray("tool_calls")->array.size() == 2);
    Q4T_CHECK(OutputJson(plan.messages).find("PLE from SSD") !=
              std::string::npos);
    ChatPlanConfig config;
    config.model = "cpu";
    ChatRequestPlan chat;
    Q4T_CHECK(PlanChatRequest(Object({{"messages", plan.messages}}), config,
                              &chat, &error));
  }
  return true;
}

Q4T_TEST(
    responses_file_search_history_missing_or_failed_results_stay_explicit) {
  for (const auto* state :
       {"completed", "in_progress", "searching", "failed", "incomplete"}) {
    auto item = Object({{"type", String("file_search_call")},
                        {"id", String("fs_history")},
                        {"status", String(state)},
                        {"queries", Array({String("missing evidence")})}});
    ResponsesInputPlan plan;
    ChatPlanError error;
    Q4T_CHECK(NormalizeResponsesInput(Object({{"input", Array({item})}}), &plan,
                                      &error));
    Json result;
    Q4T_CHECK(Parse(plan.messages.array.back().GetString("content"), &result) &&
              result.GetString("status") == state &&
              !result.GetBool("results_available", true) &&
              result.Find("results")->IsNull());
    Set(&item, "unrecognized", Boolean(true));
    Q4T_CHECK(!NormalizeResponsesInput(Object({{"input", Array({item})}}),
                                       &plan, &error));
  }
  return true;
}
