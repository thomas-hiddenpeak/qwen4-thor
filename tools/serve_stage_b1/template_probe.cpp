// CPU-only native renderer + tokenizer; no weights or CUDA runtime.
#include <iostream>
#include <memory>
#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_template.h"
#include "q4t/server/http_response.h"
#include "q4t/text/tokenizer.h"

int main(int argc, char** argv) {
  if (argc != 2) return 2;
  std::unique_ptr<q4t::text::Tokenizer> tokenizer;
  auto status = q4t::text::Tokenizer::Load(argv[1], {}, &tokenizer);
  if (!status) {
    std::cerr << status.message() << '\n';
    return 1;
  }
  std::string line;
  while (std::getline(std::cin, line)) {
    q4t::io::Json row;
    if (!q4t::io::ParseJson(line, &row, true)) return 2;
    const auto* request = row.Find("request");
    const auto* contents = row.GetArray("contents");
    if (!request || !contents) return 2;
    std::vector<std::string> text;
    for (const auto& value : contents->array) {
      if (!value.IsString()) return 2;
      text.push_back(value.str);
    }
    std::string param, prompt;
    const auto contract = q4t::server::ValidateChatContract(
        *request, "qwen3.8-flash-next", true, &param);
    const auto rendered =
        q4t::server::RenderChatPrompt(*request, text, &prompt);
    const auto* messages = request->Find("messages");
    const bool profile = messages &&
        q4t::server::ValidateChatMessageProfile(*messages).ok();
    std::cout << "{\"contract\":" << (contract ? "true" : "false")
              << ",\"profile\":" << (profile ? "true" : "false")
              << ",\"rendered\":" << (rendered ? "true" : "false")
              << ",\"param\":\""
              << q4t::server::detail::JsonEscape(param) << "\",\"prompt\":\""
              << q4t::server::detail::JsonEscape(prompt) << "\",\"ids\":[";
    if (rendered) {
      std::vector<uint32_t> ids;
      if (!tokenizer->Encode(prompt, &ids)) return 1;
      for (size_t i = 0; i < ids.size(); ++i)
        std::cout << (i ? "," : "") << ids[i];
    }
    std::cout << "]}\n";
  }
}
