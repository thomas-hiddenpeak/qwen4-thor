// Production request semantics without model/tokenizer/HTTP/GPU execution.
#include <iostream>
#include <string>
#include <vector>

#include "q4t/server/chat_plan.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/chat_response_parser.h"
#include "q4t/server/http_request.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/reasoning_options.h"
#include "q4t/server/request_json.h"
#include "q4t/server/thinking_budget.h"

int main() {
  using namespace q4t::server;
  std::string line;
  while (std::getline(std::cin, line)) {
    q4t::io::Json fixture;
    if (!q4t::io::ParseJson(line, &fixture, true)) return 2;
    const auto* raw = fixture.Find("raw");
    if (!raw || !raw->IsString()) return 2;
    ChatPlanError error;
    q4t::io::Json input;
    auto status = q4t::Status();
    if (raw->str.size() > kChatBodyBytes) {
      status = q4t::Status::Fail("request body exceeds byte limit");
      error = {"request_too_large", "body"};
    } else {
      status = q4t::io::ParseJson(raw->str, &input, true, kRequestJsonLimits);
      if (!status) error = {"invalid_json", ""};
    }
    const auto kind = fixture.GetString("kind", "chat");
    if (kind == "cancel") {
      if (status) status = ValidateCancelContract(input);
      if (!status) error = {"invalid_cancellation", ""};
    } else if (kind == "chat" || kind == "preflight") {
      const q4t::io::Json* request = &input;
      if (status && kind == "preflight") {
        status = ValidatePreflightEnvelope(input, &error);
        if (status) request = input.Find("request");
      }
      ChatPlanConfig config;
      config.model = fixture.GetString("model", "qwen3.8-flash-next");
      // These are declared configuration inputs, not backend availability
      // observations. No grammar compilation or cache access occurs here.
      config.structured = fixture.GetBool("structured", true);
      config.token_vocab = static_cast<int>(fixture.GetInt("token_vocab", 0));
      config.session_cache = fixture.GetBool("session_cache", false);
      config.prefix_cache = fixture.GetBool("prefix_cache", false);
      ChatRequestPlan plan;
      std::string prompt;
      std::vector<VisionItem> items;
      if (status) status = PlanChatRequest(*request, config, &plan, &error);
      if (status) request = &plan.Request(*request);
      if (status)
        status = PrepareChatPrompt(*request, plan.constraints, &prompt, &items,
                                   &error.param);
      if (status) {
        ChatResponseParser parser;
        status = parser.Init(*request, prompt, "contract-probe", true,
                              plan.legacy_output);
      }
      if (status) {
        ReasoningOptions reasoning;
        status = ParseReasoningOptions(*request, &reasoning, &error.param);
        ThinkingBudget thinking;
        const auto* tools = request->GetArray("tools");
        if (status)
          status = thinking.Init(
              reasoning.token_budget, prompt, plan.constraints.json_output,
              (tools && !tools->array.empty()) ||
                  plan.constraints.choice == OutputConstraints::ToolChoice::kNone);
      }
    } else {
      return 2;
    }
    std::cout << "{\"accepted\":" << (status ? "true" : "false");
    if (!status)
      std::cout << ",\"response\":"
                << detail::ErrorJson(400, status.message(), error.code,
                                     error.param);
    std::cout << "}\n";
  }
}
