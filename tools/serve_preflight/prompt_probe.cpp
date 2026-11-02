// Runs production planning and the checkpoint tokenizer without Start(),
// model loading, inference, cache leases, grammar workers, or CUDA calls.
#include <iostream>

#include "q4t/server/chat_plan.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/http_response.h"
#include "q4t/server/structured_decoder.h"
#include "q4t/text/tokenizer.h"

int main(int argc, char** argv) {
  if (argc != 2) return 2;
  using namespace q4t::server;
  std::unique_ptr<q4t::text::Tokenizer> tokenizer;
  auto status = q4t::text::Tokenizer::Load(argv[1], {}, &tokenizer);
  if (!status) {
    std::cerr << status.message() << '\n';
    return 1;
  }
  std::string line;
  while (std::getline(std::cin, line)) {
    q4t::io::Json fixture;
    if (!q4t::io::ParseJson(line, &fixture, true)) return 2;
    const auto* body = fixture.Find("body");
    if (!body) return 2;
    ChatPlanConfig config;
    config.model = "qwen3.8-flash-next";
    config.structured = kStructuredDecodingAvailable;
    config.max_len = static_cast<int>(fixture.GetInt("max_len", 8192));
    if (const auto* caps = fixture.GetArray("capacities")) {
      for (const auto& cap : caps->array)
        config.capacities.push_back(static_cast<int>(cap.AsInt()));
      config.max_seq = static_cast<int>(config.capacities.size());
    }
    ChatPlanError error;
    ChatRequestPlan plan;
    ChatPromptBudget budget;
    std::string prompt, response;
    std::vector<VisionItem> items;
    std::vector<uint32_t> tokens;
    status = ValidatePreflightEnvelope(*body, &error);
    if (status) {
      const auto& req = *body->Find("request");
      status = PlanChatRequest(req, config, &plan, &error);
      if (status)
        status = PrepareChatPrompt(req, plan.constraints, &prompt, &items,
                                   &error.param);
      if (status)
        status = EncodeChatPrompt(
            req, config, prompt,
            [&](std::string_view text, std::vector<uint32_t>* out) {
              return tokenizer->Encode(text, out);
            },
            &tokens, &budget, &error);
      if (status)
        status = ChatPreflightJson(*body, config, plan, budget, prompt, tokens,
                                   &response, &error);
    }
    if (!status)
      response =
          detail::ErrorJson(400, status.message(), error.code, error.param);
    std::cout << response << '\n';
  }
}
