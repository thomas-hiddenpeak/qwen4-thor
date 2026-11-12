#include "q4t/server/thinking_budget.h"

#include <stdexcept>
#include <string>

#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_response_parser.h"
#include "q4t/server/http_response.h"
#include "q4t/server/reasoning_options.h"
#include "q4t/test.h"

namespace {
using q4t::Status;
using q4t::io::Json;
using q4t::server::ThinkingBudget;
constexpr auto kThinking = "<|im_start|>assistant\n<think>\n";
constexpr auto kClosed = "<|im_start|>assistant\n<think>\n\n</think>\n\n";

Json Parse(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("bad test JSON");
  return value;
}

Status Encode(std::string_view text, std::vector<int32_t>* ids) {
  ids->clear();
  for (unsigned char byte : text) ids->push_back(byte + 1);
  return Status();
}

Status Decode(int32_t id, std::string* text) {
  if (id < 0 || id > 256) return Status::Fail("out of vocabulary");
  *text = id == 0 ? "" : std::string(1, static_cast<char>(id - 1));
  return Status();
}

bool Prepare(ThinkingBudget* budget, int limit, bool thinking = true,
             bool json = false, bool tools = false) {
  const int32_t stops[] = {0};
  return budget->Init(limit, thinking ? kThinking : kClosed, json, tools)
             .ok() &&
         budget->PrepareTokens(Encode, Decode, 257, stops).ok();
}

bool Close(ThinkingBudget* budget, std::string* text) {
  int count = 0;
  for (int32_t id; (id = budget->ForcedToken()) >= 0;) {
    Q4T_CHECK(++count <= 14);
    std::string piece;
    Q4T_CHECK(Decode(id, &piece));
    Q4T_CHECK(budget->Observe(id, piece));
    *text += piece;
  }
  Q4T_CHECK(!budget->Thinking());
  return true;
}
}  // namespace

Q4T_TEST(thinking_usage_tracks_unbounded_tokens_without_forcing_output) {
  q4t::server::ThinkingBudget usage;
  Q4T_CHECK(usage.Init(-1, "<think>\n", false, true, true));
  Q4T_CHECK(!usage.Enabled() && usage.Tracking() && usage.CanSpeculate(100000));
  Q4T_CHECK(usage.Observe(1, "checking"));
  Q4T_CHECK(usage.Observe(2, "</thi"));
  Q4T_CHECK(usage.Observe(3, "nk>answer"));
  Q4T_CHECK(usage.Observe(4, "<tool_call><think></tool_call>"));
  Q4T_CHECK(usage.Observe(5, "<think>again"));
  Q4T_CHECK(usage.Observe(6, "</think>"));
  Q4T_CHECK(usage.Observe(7, "", true));
  Q4T_CHECK(usage.ReasoningTokens() == 5 && usage.ForcedTokens() == 0);
  Q4T_CHECK(usage.Allows("<think>") && usage.ForcedToken() == -1);
  q4t::server::ThinkingBudget old;
  Q4T_CHECK(old.Init(-1, "arbitrary legacy prompt", false, false));
  Q4T_CHECK(!old.Tracking() && old.Observe(1, "<think>"));
  Q4T_CHECK(old.ReasoningTokens() == 0);
  return true;
}

Q4T_TEST(thinking_budget_input_exact_integer_and_modes) {
  for (const auto* wire : {"0", "1", "1e1", "2147483647", "null", "-0"}) {
    auto request =
        Parse(std::string(R"({"messages":[{"role":"user","content":"q"}],)"
                          R"("thinking_token_budget":)") +
              wire + '}');
    std::string param;
    Q4T_CHECK(q4t::server::ValidateChatContract(request, "m", false, &param));
    Q4T_CHECK(param.empty());
  }
  for (const auto* wire :
       {"true", "false", "-1", "1.5", "2147483648", "0.00000000000000000001",
        "1.0000000000000001", "\"1\"", "[]", "{}"}) {
    auto request =
        Parse(std::string(R"({"messages":[{"role":"user","content":"q"}],)"
                          R"("thinking_token_budget":)") +
              wire + '}');
    std::string param;
    Q4T_CHECK(!q4t::server::ValidateChatContract(request, "m", false, &param));
    Q4T_CHECK(param == "thinking_token_budget");
  }
  for (
      const auto* body :
      {R"("prompt":"raw")",
       R"("messages":[{"role":"user","content":"q"}],"parse_response":false)"}) {
    for (const auto* value : {"0", "null"}) {
      auto request = Parse(std::string("{") + body +
                           ",\"thinking_token_budget\":" + value + '}');
      std::string param;
      Q4T_CHECK(
          q4t::server::ValidateChatContract(request, "m", false, &param).ok() ==
          (std::string(value) == "null"));
      if (std::string(value) != "null")
        Q4T_CHECK(param == "thinking_token_budget");
    }
  }
  return true;
}

Q4T_TEST(thinking_budget_zero_and_forced_accounting) {
  ThinkingBudget budget;
  Q4T_CHECK(Prepare(&budget, 0));
  Q4T_CHECK(!budget.CanSpeculate(4));
  Q4T_CHECK(!budget.Allows("private"));
  Q4T_CHECK(!budget.Observe('x' + 1, "x"));
  Q4T_CHECK(budget.Consumed() == 0 && budget.ForcedTokens() == 0);
  std::string close;
  Q4T_CHECK(Close(&budget, &close));
  Q4T_CHECK(close == "\n</think>\n\n");
  Q4T_CHECK(budget.Consumed() == 0 && budget.ReasoningTokens() == 11);
  Q4T_CHECK(budget.Observe(1000, "answer"));
  Q4T_CHECK(budget.Observe(0, "", true));
  const auto usage =
      Parse("{\"completion_tokens\":13" + budget.UsageFields() + '}');
  Q4T_CHECK(
      usage.Find("completion_tokens_details")->GetInt("reasoning_tokens") == 11);
  Q4T_CHECK(usage.Find("q4t_thinking_budget")->GetInt("consumed") == 0);
  return true;
}

Q4T_TEST(thinking_budget_partial_close_never_duplicates_delimiter) {
  const std::string close = "</think>";
  for (size_t prefix = 0; prefix < close.size(); ++prefix) {
    ThinkingBudget budget;
    Q4T_CHECK(Prepare(&budget, 1));
    const auto text = "reason✅" + close.substr(0, prefix);
    Q4T_CHECK(budget.Observe(1000, text));
    std::string suffix;
    Q4T_CHECK(Close(&budget, &suffix));
    Q4T_CHECK(suffix == (prefix ? "" : "\n") + close.substr(prefix) + "\n\n");
    Q4T_CHECK(budget.Consumed() == 1);
    Q4T_CHECK(budget.ForcedTokens() ==
              static_cast<int64_t>(close.size() - prefix + (prefix ? 2 : 3)));
  }
  return true;
}

Q4T_TEST(thinking_budget_utf8_closure_preserves_scalars) {
  for (const auto& [prefix, completed] :
       {std::pair{std::string("\xc2"), std::string("\xc2\x80")},
        {std::string("\xe0"), std::string("\xe0\xa0\x80")},
        {std::string("\xe4\xb8"), std::string("\xe4\xb8\x80")},
        {std::string("\xed"), std::string("\xed\x80\x80")},
        {std::string("\xf0"), std::string("\xf0\x90\x80\x80")},
        {std::string("\xf4\x8f\xbf"), std::string("\xf4\x8f\xbf\x80")}}) {
    ThinkingBudget budget;
    Q4T_CHECK(Prepare(&budget, 1));
    Q4T_CHECK(budget.Observe(1000, prefix));
    std::string suffix;
    Q4T_CHECK(Close(&budget, &suffix));
    Q4T_CHECK(prefix + suffix == completed + "\n</think>\n\n");
    Q4T_CHECK(q4t::server::detail::NormalizeUtf8(prefix + suffix) ==
              prefix + suffix);
    Q4T_CHECK(budget.ForcedTokens() <= 14);
  }
  return true;
}

Q4T_TEST(thinking_budget_natural_close_reentry_and_literals) {
  ThinkingBudget budget;
  Q4T_CHECK(Prepare(&budget, 3));
  Q4T_CHECK(budget.Observe(1000, "why</think>"));
  Q4T_CHECK(budget.Consumed() == 1 && budget.ForcedToken() < 0);
  Q4T_CHECK(budget.Observe(1000, "answer<th"));
  Q4T_CHECK(budget.Consumed() == 1);
  Q4T_CHECK(budget.Observe(1000, "ink>more"));
  Q4T_CHECK(budget.Observe(1000, "</think>"));
  Q4T_CHECK(budget.Consumed() == 3 && budget.ForcedTokens() == 0);
  Q4T_CHECK(budget.Observe(1000, "partial<th"));
  Q4T_CHECK(!budget.Allows("ink>private</think>"));
  Q4T_CHECK(!budget.Observe(1000, "ink>private</think>"));
  Q4T_CHECK(budget.Allows("is is literal"));
  Q4T_CHECK(budget.Observe(1000, "is is literal"));
  ThinkingBudget json, tools;
  Q4T_CHECK(Prepare(&json, 0, false, true));
  Q4T_CHECK(json.Allows(R"({"example":"<think>literal</think>"})"));
  Q4T_CHECK(json.Observe(1000, R"({"example":"<think>literal</think>"})"));
  Q4T_CHECK(Prepare(&tools, 0, false, false, true));
  Q4T_CHECK(tools.Observe(1000, "<tool_call><parameter=text>"));
  Q4T_CHECK(tools.Allows("<think>literal</think>"));
  Q4T_CHECK(
      tools.Observe(1000, "<think>literal</think></parameter></tool_call>"));
  Q4T_CHECK(!tools.Allows("<think>private"));
  return true;
}

Q4T_TEST(thinking_budget_matches_parser_across_all_chunks) {
  const auto request = Parse(R"({"messages":[],"tools":[{"type":"function",
      "function":{"name":"noop"}}]})");
  const std::string text =
      "reason✅<tool_call>example</tool_call></think>answer"
      "<tool_call><function=noop><parameter=text><think>literal</think>"
      "</parameter></function></tool_call>tail<think>again</think>end";
  for (size_t split = 0; split <= text.size(); ++split) {
    ThinkingBudget budget;
    Q4T_CHECK(Prepare(&budget, 1000, true, false, true));
    q4t::server::ChatResponseParser parser;
    q4t::server::detail::Utf8Stream utf8;
    std::vector<q4t::server::ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(request, kThinking, "r", true));
    for (const auto& part : {text.substr(0, split), text.substr(split)}) {
      Q4T_CHECK(budget.Observe(1000, part));
      Q4T_CHECK(parser.Append(utf8.Append(part), &deltas));
      Q4T_CHECK(parser.Thinking() == budget.Thinking());
    }
    Q4T_CHECK(parser.Finish(false, &deltas));
  }
  return true;
}

Q4T_TEST(thinking_budget_speculation_reserves_pending_token) {
  for (int limit = 1; limit <= 64; ++limit) {
    for (int accepted = 1; accepted <= 9; ++accepted) {
      ThinkingBudget budget;
      Q4T_CHECK(Prepare(&budget, limit));
      while (budget.CanSpeculate(accepted)) {
        for (int i = 0; i < accepted; ++i)
          Q4T_CHECK(budget.Observe('x' + 1, "x"));
        Q4T_CHECK(budget.Consumed() < limit);
      }
      // The already computed MTP correction must be admitted without changing
      // its prefix/KV; only subsequent host-logit choices may force closure.
      Q4T_CHECK(budget.Observe('p' + 1, "p"));
      while (budget.Consumed() < limit) Q4T_CHECK(budget.Observe('x' + 1, "x"));
      std::string closure;
      Q4T_CHECK(Close(&budget, &closure));
      Q4T_CHECK(budget.Consumed() == limit);
    }
  }
  return true;
}

Q4T_TEST(thinking_budget_bad_closure_and_total_limit_fail_closed) {
  ThinkingBudget invalid;
  Q4T_CHECK(invalid.Init(0, kThinking, false, false));
  const int32_t stops[] = {0};
  Q4T_CHECK(!invalid.PrepareTokens(
      Encode,
      [](int32_t, std::string* text) {
        *text = "wrong";
        return Status();
      },
      257, stops));
  Q4T_CHECK(!invalid.Observe(1, "x"));
  for (bool strict : {false, true}) {
    auto request = Parse(strict ? R"({"messages":[],"response_format":{
        "type":"json_object"}})"
                                : R"({"messages":[]})");
    ThinkingBudget budget;
    Q4T_CHECK(Prepare(&budget, 0, true, strict));
    q4t::server::ChatResponseParser parser;
    std::vector<q4t::server::ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(request, kThinking, "r", true));
    const int32_t token = budget.ForcedToken();
    std::string text;
    Q4T_CHECK(Decode(token, &text));
    Q4T_CHECK(budget.Observe(token, text));
    Q4T_CHECK(parser.Append(text, &deltas));
    // The single total output token cannot be enlarged to finish </think>.
    Q4T_CHECK(parser.Finish(true, &deltas).ok() == !strict);
    Q4T_CHECK(budget.ForcedTokens() == 1 && budget.Consumed() == 0);
    Q4T_CHECK(budget.Thinking());
    // Incremental reasoning is provisional, including under a final schema.
    // Failure must never certify a content value or completed tool call.
    if (strict)
      for (const auto& delta : deltas)
        Q4T_CHECK(delta.kind == q4t::server::ResponseDelta::Kind::kReasoning);
    Q4T_CHECK(parser.Content().empty() && !parser.HasToolCalls());
  }
  return true;
}
