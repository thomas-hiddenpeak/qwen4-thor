// Post-E2E model-template byte check. Not a preflight test or benchmark.
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/chat_template.h"

int main(int argc, char** argv) {
  if (argc != 2) return 2;
  std::ifstream input(argv[1]);
  if (!input) return 2;
  int count = 0;
  int failed = 0;
  std::string line;
  while (std::getline(input, line)) {
    q4t::io::Json row;
    auto status = q4t::io::ParseJson(line, &row, true);
    if (!status) return 2;
    const auto* request = row.Find("request");
    const auto* content = row.GetArray("contents");
    const auto* expected = row.Find("expected");
    if (!request || !content || !expected || !expected->IsString()) return 2;
    std::vector<std::string> contents;
    for (const auto& part : content->array) {
      if (!part.IsString()) return 2;
      contents.push_back(part.str);
    }
    std::string rendered;
    status = q4t::server::RenderChatPrompt(*request, contents, &rendered);
    ++count;
    if (!status || rendered != expected->str) {
      ++failed;
      std::cerr << row.GetString("id") << ": mismatch; status="
                << status.message() << ", expected_bytes="
                << expected->str.size() << ", actual_bytes=" << rendered.size()
                << '\n';
    }
  }
  std::cout << "cases=" << count << " failed=" << failed << '\n';
  return count && !failed ? 0 : 1;
}
