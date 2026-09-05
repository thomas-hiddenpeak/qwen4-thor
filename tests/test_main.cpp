// Test runner entry point for q4t_tests.
//
// Usage:
//   q4t_tests                 run all tests
//   q4t_tests <name-substring> run only tests whose name contains the substring
//
// The filter is useful for running a single heavy test (e.g. the 48-layer
// reference dump) without loading the full model for every other test.
#include "q4t/test.h"

#include <cstring>
#include <string>

int main(int argc, char** argv) {
  std::string filter = (argc > 1) ? argv[1] : "";
  if (filter.empty()) {
    return q4t::test::RunAllTests();
  }
  int failures = 0;
  int ran = 0;
  for (const auto& c : q4t::test::Registry()) {
    if (c.name.find(filter) == std::string::npos) continue;
    ++ran;
    bool ok = c.fn();
    std::printf("[%s] %s\n", ok ? "PASS" : "FAIL", c.name.c_str());
    if (!ok) ++failures;
  }
  std::printf("%d test(s) matched '%s', %d failed\n", ran, filter.c_str(),
              failures);
  return failures;
}
