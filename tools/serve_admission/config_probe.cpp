// CPU-only production CLI/limit/configuration contract. Values are hypothetical
// effective capacities; no tokenizer, weights, model startup, GPU or HTTP.
#include <iostream>
#include <string_view>
#include <vector>

#include "q4t/server/chat_plan.h"
#include "q4t/server/http_response.h"
#include "q4t/server/structured_decoder.h"

int main() {
  using namespace q4t::server;
  std::string line;
  while (std::getline(std::cin, line)) {
    q4t::io::Json fixture;
    if (!q4t::io::ParseJson(line, &fixture, true)) return 2;
    const auto* arguments = fixture.GetArray("argv");
    if (!arguments) return 2;
    std::vector<std::string_view> argv;
    for (const auto& argument : arguments->array) {
      if (!argument.IsString()) return 2;
      argv.push_back(argument.str);
    }
    ServerOptions options;
    options.model_dir = "/hypothetical-read-only-model";
    auto status = ParseServerOptions(argv, &options);
    ChatPlanConfig config;
    config.model = "qwen3.8-flash-next";
    config.max_seq = static_cast<int>(fixture.GetInt("effective_slots", 1));
    if (status)
      status =
          ResolveAdmissionLimits(options, config.max_seq, &config.admission);
    if (!status) {
      std::cout << "{\"accepted\":false,\"error\":"
                << detail::ErrorJson(400, status.message(), "invalid_options")
                << "}\n";
      continue;
    }
    config.default_max_tokens = options.max_tokens;
    config.request_deadline_ms = options.request_deadline_ms;
    config.structured = kStructuredDecodingAvailable;
    std::cout << "{\"accepted\":true,\"configuration\":"
              << EffectiveServeConfigJson(options, config, true,
                                          !options.no_mtp,
                                          std::max(128, 8 * config.max_seq))
              << "}\n";
  }
}
