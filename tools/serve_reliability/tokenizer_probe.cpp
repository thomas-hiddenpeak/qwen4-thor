// CPU-only differential driver. Compile unchanged against parent and candidate.
#include "q4t/io/json.h"
#include "q4t/text/tokenizer.h"

#include <iostream>
#include <memory>
#include <string>
#include <vector>

int main(int argc, char** argv) {
  if (argc != 2) return 2;
  std::unique_ptr<q4t::text::Tokenizer> tokenizer;
  const auto loaded = q4t::text::Tokenizer::Load(argv[1], {}, &tokenizer);
  if (!loaded.ok()) {
    std::cerr << loaded.message() << '\n';
    return 1;
  }
  std::string line;
  while (std::getline(std::cin, line)) {
    q4t::io::Json input;
    if (!q4t::io::ParseJson(line, &input).ok() || !input.IsString()) return 2;
    std::vector<uint32_t> ids;
    const auto encoded = tokenizer->Encode(input.str, &ids);
    if (!encoded.ok()) {
      std::cerr << encoded.message() << '\n';
      return 1;
    }
    std::cout << '[';
    for (size_t i = 0; i < ids.size(); ++i)
      std::cout << (i ? "," : "") << ids[i];
    std::cout << "]\n";
  }
}
