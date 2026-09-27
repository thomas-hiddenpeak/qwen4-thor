// Strict by default. --required-list selects exact names and forbids skips.
#include "q4t/test.h"

#include <fstream>
#include <string>

int main(int argc, char** argv) {
  std::string filter, required;
  bool list = false, allow_skips = false, has_filter = false;
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--list" && !list) {
      list = true;
    } else if (arg == "--allow-skips" && !allow_skips) {
      allow_skips = true;
    } else if (arg == "--required-list" && required.empty() && i + 1 < argc) {
      required = argv[++i];
      if (required.empty()) return 2;
    } else if (!arg.starts_with('-') && !has_filter) {
      filter = arg;
      has_filter = true;
    } else {
      std::fprintf(stderr, "Invalid test arguments\n");
      return 2;
    }
  }
  if ((!required.empty() && (has_filter || allow_skips || list)) ||
      (list && (has_filter || allow_skips))) return 2;
  if (list) {
    for (const auto& c : q4t::test::Registry())
      std::printf("%s\n", c.name.c_str());
    return q4t::test::Registry().empty() ? 2 : 0;
  }
  std::vector<std::string> names;
  if (!required.empty()) {
    std::ifstream input(required);
    if (!input) return 2;
    std::string name;
    while (std::getline(input, name)) {
      if (!name.empty() && name.back() == '\r') name.pop_back();
      if (!name.empty() && !name.starts_with('#')) names.push_back(name);
    }
    if (input.bad()) return 2;
  } else {
    for (const auto& c : q4t::test::Registry())
      if (c.name.find(filter) != std::string::npos) names.push_back(c.name);
  }
  return q4t::test::RunTests(names, allow_skips);
}
