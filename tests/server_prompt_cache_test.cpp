#include "q4t/server/prompt_cache_policy.h"

#include <algorithm>
#include <numeric>

#include "q4t/server/chat_prompt.h"
#include "q4t/server/prompt_cache_diagnostics.h"
#include "q4t/server/session_cache.h"
#include "q4t/server/responses_plan.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_input.h"
#include "q4t/test.h"

namespace {
using q4t::io::Json;
using namespace q4t::server;
Json Read(const std::string& text) {
  Json value;
  const auto status = q4t::io::ParseJson(text, &value, true, kSchemaJsonLimits);
  if (!status) throw std::runtime_error(status.message());
  return value;
}
using Kind = PromptCacheBoundary::Kind;
}  // namespace

Q4T_TEST(prompt_cache_retention_bounds_and_modern_ttl_are_independent) {
  PromptCacheOptions options;
  std::string param;
  Q4T_CHECK(ParsePromptCacheOptions(
      Read(R"({"prompt_cache_retention":"in_memory"})"), &options, &param));
  auto lifetime = PromptCacheLifetime(options, 1800, false);
  Q4T_CHECK(lifetime.write_seconds == 1800 && lifetime.max_age_seconds == 3600);
  Q4T_CHECK(ParsePromptCacheOptions(Read(R"({"prompt_cache_retention":"24h"})"),
                                    &options, &param));
  Q4T_CHECK(PromptCacheLifetime(options, 1800, false).max_age_seconds == 86400);
  Q4T_CHECK(
      ParsePromptCacheOptions(Read(R"({"prompt_cache_retention":"in_memory",
               "prompt_cache_options":{"ttl":"12h"}})"),
                              &options, &param));
  lifetime = PromptCacheLifetime(options, 43200, true);
  Q4T_CHECK(lifetime.write_seconds == 43200 &&
            lifetime.max_age_seconds == 86400 &&
            lifetime.read_refresh_seconds == 1800);
  return true;
}

Q4T_TEST(prompt_cache_diagnostics_isolate_clients_and_require_actual_restore) {
  PromptCacheDiagnostics cache;
  const auto settings = Read(R"({"model":"cpu","prompt_cache_key":"one"})");
  std::vector<int32_t> tokens(2048, 42);
  const auto observed =
      PromptCacheDiagnostics::Observe(settings, tokens, 1501, false);
  Q4T_CHECK(observed && observed->blocks.size() == 12);
  Q4T_CHECK(cache.Remember("client-a", "resp_one", *observed));
  Q4T_CHECK(!cache.Remember("client-a", "resp_one", *observed));
  Q4T_CHECK(
      cache.Compare("client-b", "resp_one", settings, tokens, 1501, true, false)
          .GetString("type") == "comparison_response_not_found");
  Q4T_CHECK(
      cache.Compare("client-a", "resp_one", settings, tokens, 0, true, false)
          .GetString("type") == "unavailable");
  Q4T_CHECK(
      cache
          .Compare("client-a", "resp_one", settings, tokens, 1501, false, false)
          .GetString("type") == "unavailable");
  tokens[1501] = 99;  // A changed suffix is outside the reusable prefix.
  Q4T_CHECK(
      cache.Compare("client-a", "resp_one", settings, tokens, 1501, true, false)
          .GetString("type") == "cache_hit");
  tokens[1499] = 99;
  const auto miss = cache.Compare("client-a", "resp_one", settings, tokens,
                                  1408, true, false);
  Q4T_CHECK(miss.GetString("reason") == "input_changed" &&
            miss.GetInt("comparison_reusable_tokens") == 1501 &&
            miss.GetInt("cache_missed_tokens") == 93);
  const auto compact =
      cache.Compare("client-a", "resp_one", settings, tokens, 0, true, true);
  Q4T_CHECK(compact.GetString("reason") == "context_compacted");
  return true;
}

Q4T_TEST(prompt_cache_diagnostics_classify_settings_and_expire_without_io) {
  PromptCacheDiagnostics cache;
  const auto settings = Read(R"({"model":"cpu","prompt_cache_key":"one",
    "service_tier":"default","tools":[{"name":"read","type":"function"}],
    "text":{"format":{"type":"text"},"verbosity":"medium"},
    "reasoning":{"effort":"none"}})");
  std::vector<int32_t> tokens(2048, 42);
  const auto now = PromptCacheDiagnostics::Clock::now();
  const auto observation =
      PromptCacheDiagnostics::Observe(settings, tokens, 1408, false);
  Q4T_CHECK(observation && cache.Remember("a", "one", *observation, now));
  for (const auto& [change, expected] :
       std::vector<std::pair<std::string, std::string>>{
           {R"({"model":"different"})", "model_changed"},
           {R"({"prompt_cache_key":"different"})", "prompt_cache_key_changed"},
           {R"({"service_tier":"flex"})", "service_tier_changed"},
           {R"({"tools":[]})", "tools_changed"},
           {R"({"text":{"format":{"type":"json_object"}}})",
            "text_format_changed"},
           {R"({"reasoning":{"effort":"low"}})", "reasoning_effort_changed"},
           {R"({"text":{"format":{"type":"text"},"verbosity":"high"}})",
            "verbosity_changed"}}) {
    auto changed = settings;
    const auto patch = Read(change);
    for (auto& field : changed.object)
      if (const auto* value = patch.Find(field.first)) field.second = *value;
    const auto result =
        cache.Compare("a", "one", changed, tokens, 0, true, false, now);
    Q4T_CHECK(result.GetString("reason") == expected &&
              result.GetInt("cache_missed_tokens") == 1408);
  }
  const auto reordered = Read(R"({"reasoning":{"effort":"none"},
    "text":{"verbosity":"medium","format":{"type":"text"}},
    "tools":[{"type":"function","name":"read"}],"service_tier":"default",
    "prompt_cache_key":"one","model":"cpu"})");
  Q4T_CHECK(cache.Compare("a", "one", reordered, tokens, 1408, true, false, now)
                .GetString("type") == "cache_hit");
  Q4T_CHECK(cache
                .Compare("a", "one", settings, tokens, 1408, true, false,
                         now + std::chrono::hours(1))
                .GetString("type") == "comparison_response_not_found");
  return true;
}

Q4T_TEST(prompt_cache_diagnostics_bound_total_and_per_client_history) {
  PromptCacheDiagnostics cache;
  const auto settings = Read(R"({"model":"cpu"})");
  const std::vector<int32_t> tokens(1024, 7);
  const auto observation =
      PromptCacheDiagnostics::Observe(settings, tokens, 1024, false);
  Q4T_CHECK(observation);
  const auto now = PromptCacheDiagnostics::Clock::now();
  for (int i = 0; i < 17; ++i)
    Q4T_CHECK(cache.Remember("a", std::to_string(i), *observation,
                             now + std::chrono::seconds(i)));
  Q4T_CHECK(cache
                .Compare("a", "0", settings, tokens, 1024, true, false,
                         now + std::chrono::seconds(17))
                .GetString("type") == "comparison_response_not_found");
  Q4T_CHECK(cache
                .Compare("a", "1", settings, tokens, 1024, true, false,
                         now + std::chrono::seconds(17))
                .GetString("type") == "cache_hit");
  for (int i = 0; i < 128; ++i)
    Q4T_CHECK(cache.Remember("b" + std::to_string(i), "one", *observation,
                             now + std::chrono::seconds(20 + i)));
  Q4T_CHECK(cache
                .Compare("a", "16", settings, tokens, 1024, true, false,
                         now + std::chrono::seconds(160))
                .GetString("type") == "comparison_response_not_found");
  Q4T_CHECK(!PromptCacheDiagnostics::Observe(settings, tokens, 1025, false));
  return true;
}

Q4T_TEST(prompt_cache_adaptation_offsets_and_zero_output_slot_budget) {
  ChatPlanConfig config;
  config.model = "cpu";
  config.max_len = 2048;
  config.capacities = {2048};
  const auto request = Read(R"({"input":[{"role":"user","content":[
    {"type":"input_text","text":"prefix","prompt_cache_breakpoint":{"mode":"explicit"}},
    {"type":"input_text","text":"suffix"}]}],
    "reasoning":{"effort":"minimal"},"text":{"verbosity":"high"},
    "prompt_cache_options":{"prewarm":true}})");
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan generation;
  ChatPlanError error;
  Q4T_CHECK(ParseResponsesEnvelope(request, config, &envelope, &error));
  Q4T_CHECK(PrepareResponsesGeneration(envelope, *request.Find("input"), config,
                                       &generation, &error));
  Q4T_CHECK(generation.input.cache_boundaries.size() == 1 &&
            generation.input.cache_boundaries[0].message_index == 1 &&
            generation.input.cache_boundaries[0].part_index == 0);
  const auto& chat = generation.chat.Request(generation.generation_request);
  Q4T_CHECK(chat.GetArray("messages")->array[1].GetString("role") == "user");
  ChatPromptBudget budget;
  Q4T_CHECK(!PlanChatBudget(chat, config, 2000, &budget, &error));
  config.prewarm_only = true;
  Q4T_CHECK(PlanChatBudget(chat, config, 2000, &budget, &error));
  Q4T_CHECK(budget.required_capacity == 2000 && budget.total_output == 0 &&
            budget.available_output == 0);
  return true;
}

Q4T_TEST(prompt_cache_policy_applies_mode_lifetime_key_and_prewarm) {
  PromptCacheOptions options;
  std::string param;
  Q4T_CHECK(ParsePromptCacheOptions(
      Read(R"({"prompt_cache_key":"客户一","prompt_cache_retention":"24h",
        "prompt_cache_options":{"mode":"explicit","ttl":"12h",
          "prewarm":true,"comparison_response_id":"resp_old"}})"),
      &options, &param));
  Q4T_CHECK(options.requested && options.explicit_only && options.prewarm);
  Q4T_CHECK(options.ttl_seconds == 43200 && options.retention == "24h" &&
            options.key == "客户一" &&
            options.comparison_response_id == "resp_old");
  const auto projected = PublicPromptCacheOptions(options);
  Q4T_CHECK(projected.GetString("ttl") == "12h" &&
            projected.GetString("mode") == "explicit" &&
            !projected.Has("prewarm"));
  Q4T_CHECK(ParsePromptCacheOptions(Read("{}"), &options, &param));
  Q4T_CHECK(!options.requested && !options.explicit_only && !options.prewarm &&
            options.ttl_seconds == 1800 && options.key.empty());
  return true;
}

Q4T_TEST(prompt_cache_chat_standard_controls_and_legacy_scope_conflicts) {
  ChatPlanConfig config;
  config.model = "cpu";
  config.prefix_cache = true;
  ChatRequestPlan plan;
  ChatPlanError error;
  auto request = Read(R"({"model":"cpu","messages":[
    {"role":"user","content":"hello"}],"prompt_cache_key":"客户",
    "prompt_cache_retention":"24h"})");
  Q4T_CHECK(PlanChatRequest(request, config, &plan, &error));
  Q4T_CHECK(plan.cache.requested && plan.cache.key == "客户");
  auto native = Read("\"" + std::string(64, 'a') + "\"");
  request.object.emplace_back("prefix_cache_scope", native);
  Q4T_CHECK(!PlanChatRequest(request, config, &plan, &error));
  Q4T_CHECK(error.param == "prefix_cache_scope");
  for (auto& [name, value] : request.object)
    if (name == "prompt_cache_key" || name == "prompt_cache_retention")
      value = {};
  Q4T_CHECK(PlanChatRequest(request, config, &plan, &error));
  Q4T_CHECK(!plan.cache.requested);
  Q4T_CHECK(PlanChatRequest(Read(R"({"model":"cpu","prompt":"raw text",
    "prompt_cache_key":"raw"})"),
                            config, &plan, &error));
  return true;
}

Q4T_TEST(prompt_cache_policy_rejects_malformed_controls_and_boundaries) {
  PromptCacheOptions options;
  std::string param;
  for (const auto* body :
       {R"({"prompt_cache_options":[]})",
        R"({"prompt_cache_options":{"mode":"disabled"}})",
        R"({"prompt_cache_options":{"ttl":"24h"}})",
        R"({"prompt_cache_options":{"ttl":null}})",
        R"({"prompt_cache_options":{"prewarm":1}})",
        R"({"prompt_cache_options":{"ignore":true}})",
        R"({"prompt_cache_options":{"comparison_response_id":"../other"}})",
        R"({"prompt_cache_retention":false})", R"({"prompt_cache_key":[]})"}) {
    Q4T_CHECK(!ParsePromptCacheOptions(Read(body), &options, &param));
    Q4T_CHECK(param.starts_with("prompt_cache"));
  }
  for (const auto* body :
       {"{}", R"({"mode":"implicit"})", R"({"mode":"explicit","ttl":false})",
        R"({"mode":"explicit","extra":1})"}) {
    bool present = false;
    int ttl = 0;
    const auto value = Read(body);
    Q4T_CHECK(!ParsePromptCacheBreakpoint(&value, "input[0].content[1]",
                                          &present, &ttl, &param));
    Q4T_CHECK(!present && param.starts_with("input[0].content[1]"));
  }
  return true;
}

Q4T_TEST(prompt_cache_explicit_empty_disables_reads_and_writes) {
  PromptCacheOptions options;
  options.explicit_only = true;
  PromptCachePlan plan;
  const std::vector<PromptCacheBoundary> points = {
      {1200, 0, Kind::kInitialInstructions}, {2400, 0, Kind::kEligibleMessage}};
  Q4T_CHECK(PlanPromptCache(options, points, 2500, &plan));
  Q4T_CHECK(plan.reads.empty() && plan.writes.empty());
  options.explicit_only = false;
  Q4T_CHECK(PlanPromptCache(options, points, 2500, &plan));
  Q4T_CHECK(plan.reads == std::vector<int>({1200, 2400}));
  Q4T_CHECK(plan.writes.size() == 1 && plan.writes[0].tokens == 2400);
  return true;
}

Q4T_TEST(prompt_cache_lookup_preserves_first_and_recent_explicit_boundaries) {
  PromptCacheOptions options;
  options.explicit_only = true;
  std::vector<PromptCacheBoundary> points;
  for (int i = 0; i < 90; ++i)
    points.push_back({1024 + i * 10, 0, Kind::kExplicit});
  PromptCachePlan plan;
  Q4T_CHECK(PlanPromptCache(options, points, 4096, &plan));
  Q4T_CHECK(plan.reads.size() == 52 && plan.reads[0] == 1024 &&
            plan.reads[1] == 1034 && plan.reads[2] == 1424 &&
            plan.reads.back() == 1914);
  Q4T_CHECK(plan.writes.size() == 4 && plan.writes.front().tokens == 1884 &&
            plan.writes.back().tokens == 1914);
  return true;
}

Q4T_TEST(prompt_cache_writes_share_longer_covering_lifetime_and_four_slots) {
  PromptCacheOptions options;
  const std::vector<PromptCacheBoundary> points = {
      {500, 0, Kind::kExplicit},     {1100, 0, Kind::kExplicit},
      {1200, 1800, Kind::kExplicit}, {1300, 43200, Kind::kExplicit},
      {1400, 0, Kind::kExplicit},    {1500, 0, Kind::kEligibleMessage}};
  PromptCachePlan plan;
  Q4T_CHECK(PlanPromptCache(options, points, 1600, &plan));
  Q4T_CHECK(plan.writes.size() == 4 && plan.writes[0].tokens == 1200);
  Q4T_CHECK(plan.writes[0].ttl_seconds == 43200 &&
            plan.writes[1].ttl_seconds == 43200 &&
            plan.writes[2].ttl_seconds == 1800 &&
            plan.writes[3].tokens == 1500);
  return true;
}

Q4T_TEST(prompt_cache_source_offsets_never_cross_partial_token_or_nfc_cluster) {
  const std::vector<size_t> starts = {0, 0, 0, 3, 7};
  int tokens = -1;
  for (const auto& [byte, count] : std::vector<std::pair<size_t, int>>{
           {0, 0}, {1, 0}, {2, 0}, {3, 3}, {5, 3}, {7, 4}, {8, 5}}) {
    Q4T_CHECK(CompletePromptPrefix(starts, 8, byte, &tokens));
    Q4T_CHECK(tokens == count);
  }
  Q4T_CHECK(!CompletePromptPrefix(starts, 8, 9, &tokens));
  Q4T_CHECK(tokens == 0);
  return true;
}

Q4T_TEST(prompt_cache_rendered_layout_preserves_unicode_trim_and_prompt_bytes) {
  const auto request = Read(R"({"enable_thinking":false,"messages":[
    {"role":"developer","content":"　repeat　"},
    {"role":"user","content":[{"type":"text","text":"  repeat"},
      {"type":"text","text":"尾部　"}]}]})");
  std::string plain, mapped, param, code;
  std::vector<VisionItem> media;
  ChatPromptLayout layout;
  Q4T_CHECK(PrepareChatPrompt(request, {}, &plain, &media, &param));
  Q4T_CHECK(
      PrepareChatPrompt(request, {}, &mapped, &media, &param, &code, &layout));
  Q4T_CHECK(plain == mapped && layout.messages.size() == 2);
  Q4T_CHECK(layout.messages[0].source_begin == 3 &&
            layout.messages[0].source_size == 6);
  Q4T_CHECK(mapped.substr(layout.messages[0].prompt_begin, 6) == "repeat");
  Q4T_CHECK(mapped.substr(layout.messages[1].prompt_begin, 12) == "repeat尾部");
  Q4T_CHECK(layout.messages[1].prompt_begin > layout.messages[0].prompt_end);
  Q4T_CHECK(layout.part_ends[1] == std::vector<size_t>({8, 17}));
  return true;
}

Q4T_TEST(prompt_cache_input_keeps_boundaries_out_of_model_fields) {
  const auto request = Read(R"({"instructions":"rules","input":[
    {"role":"user","content":[{"type":"input_text","text":"first",
      "prompt_cache_breakpoint":{"mode":"explicit","ttl":"12h"}},
      {"type":"input_text","text":"second"}]}]})");
  ResponsesInputPlan input;
  ChatPlanError error;
  Q4T_CHECK(NormalizeResponsesInput(request, &input, &error));
  Q4T_CHECK(input.cache_boundaries.size() == 1);
  const auto& mark = input.cache_boundaries[0];
  Q4T_CHECK(mark.message_index == 1 && mark.part_index == 0 &&
            mark.ttl_seconds == 43200);
  Q4T_CHECK(!input.messages.array[1].GetArray("content")->array[0].Has(
      "prompt_cache_breakpoint"));
  Q4T_CHECK(input.items.array[0].GetArray("content")->array[0].Has(
      "prompt_cache_breakpoint"));
  return true;
}

Q4T_TEST(
    prompt_cache_layout_resolves_content_breakpoint_before_changing_suffix) {
  const auto request = Read(
      "{\"enable_thinking\":false,\"messages\":[{\"role\":\"user\","
      "\"content\":[{\"type\":\"text\",\"text\":\"  " +
      std::string(1100, 'a') +
      "\"},{\"type\":\"text\",\"text\":\"uncached question\"}]}]}");
  ChatPromptLayout layout;
  std::string prompt, param;
  std::vector<VisionItem> media;
  Q4T_CHECK(PrepareChatPrompt(request, {}, &prompt, &media, &param, nullptr,
                              &layout));
  // Byte-token fixture isolates exact boundary placement from BPE semantics.
  std::vector<size_t> offsets(prompt.size());
  std::iota(offsets.begin(), offsets.end(), 0);
  PromptCacheOptions options;
  options.explicit_only = true;
  const std::vector<PromptCacheInputBoundary> marks = {{0, 0, 43200}};
  PromptCachePlan plan;
  Q4T_CHECK(PlanPromptCacheLayout(options, *request.Find("messages"), layout,
                                  marks, offsets, prompt.size(), &plan));
  Q4T_CHECK(plan.writes.size() == 1 && plan.reads.size() == 1);
  Q4T_CHECK(plan.writes[0].tokens ==
            static_cast<int>(prompt.find("uncached question")));
  Q4T_CHECK(plan.writes[0].ttl_seconds == 43200);
  return true;
}
