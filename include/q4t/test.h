// Minimal test framework for q4t.
//
// Tests register themselves via a static initializer and are run by
// RunAllTests(). No external dependency; keeps the build self-contained.
// A test returns true on success, false on failure.
#pragma once

#include <cstdio>
#include <functional>
#include <string>
#include <vector>

namespace q4t {
namespace test {

struct Case {
  std::string name;
  std::function<bool()> fn;
};

inline std::vector<Case>& Registry() {
  static std::vector<Case> cases;
  return cases;
}

inline void Register(const std::string& name, std::function<bool()> fn) {
  Registry().push_back({name, std::move(fn)});
}

// Run all registered tests. Returns the number of failures.
inline int RunAllTests() {
  int failures = 0;
  for (const auto& c : Registry()) {
    bool ok = c.fn();
    std::printf("[%s] %s\n", ok ? "PASS" : "FAIL", c.name.c_str());
    if (!ok) ++failures;
  }
  std::printf("%zu tests, %d failed\n", Registry().size(), failures);
  return failures;
}

}  // namespace test
}  // namespace q4t

#define Q4T_TEST(name)                                                   \
  static bool q4t_test_fn_##name();                                      \
  static const bool q4t_test_reg_##name = [] {                           \
    ::q4t::test::Register(#name, q4t_test_fn_##name);                    \
    return true;                                                         \
  }();                                                                   \
  static bool q4t_test_fn_##name()

#define Q4T_CHECK(cond)                                                    \
  do {                                                                     \
    if (!(cond)) {                                                         \
      std::printf("  CHECK failed: %s (line %d)\n", #cond, __LINE__);      \
      return false;                                                        \
    }                                                                      \
  } while (0)
