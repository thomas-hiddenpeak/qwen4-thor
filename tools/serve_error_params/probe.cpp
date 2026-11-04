// Differential production validation, planning and template output; no GPU.
#include <iostream>
#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/chat_plan.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/http_response.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/request_json.h"

int main() {
  using namespace q4t::server;
  std::string line;
  while (std::getline(std::cin, line)) {
    q4t::io::Json request;
    auto status = q4t::io::ParseJson(line, &request, true, kRequestJsonLimits);
    if (!status) return 2;
    ChatPlanConfig config;
    config.model = "qwen3.8-flash-next";
    config.structured = true;
    ChatRequestPlan plan;
    ChatPlanError error;
    status = PlanChatRequest(request, config, &plan, &error);
    std::string prompt;
    std::vector<VisionItem> items;
    if (status)
      status = PrepareChatPrompt(request, plan.constraints, &prompt, &items,
                                 &error.param);
    std::cout << "{\"accepted\":" << (status ? "true" : "false");
    if (!status) {
      std::cout << ",\"response\":"
                << detail::ErrorJson(400, status.message(), error.code,
                                     error.param);
    } else {
      std::cout << ",\"prompt\":\"" << detail::JsonEscape(prompt)
                << "\",\"grammar\":\""
                << detail::JsonEscape(plan.constraints.structural_tag) << '"';
    }
    std::cout << "}\n";
  }
}
