// Minimal test framework for q4t.
//
// Tests register themselves via a static initializer and are run by
// RunAllTests(). No external dependency; keeps the build self-contained.
// A test returns true on success, false on failure.
#pragma once

#include <cstdio>
#include <cstdlib>
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

// RAII: temporarily force the shared-state GDN prefill kernel (Q4T_GDN_REG=0)
// for a test's scope, restoring the prior value on exit (including early
// returns). Used by the batched / multi-seq / MTP equivalence tests: their
// single-sequence ModelPrefill golden must run the SAME GDN kernel as the
// batched path they validate. With the register-state kernel ON by default,
// the single-seq golden (register) and the batched path (shared) are both
// correct but differ numerically, and a 2-layer-model MoE routing boundary can
// flip the argmax — a false failure. Forcing both to the shared kernel restores
// the bit-identical equivalence these tests assert. (reg is covered separately
// by linear_attention.)
class GdnRegOff {
 public:
  GdnRegOff() {
    const char* prev = std::getenv("Q4T_GDN_REG");
    if (prev) {
      had_ = true;
      prev_ = prev;
    }
    setenv("Q4T_GDN_REG", "0", 1);
  }
  ~GdnRegOff() {
    if (had_)
      setenv("Q4T_GDN_REG", prev_.c_str(), 1);
    else
      unsetenv("Q4T_GDN_REG");
  }

 private:
  bool had_ = false;
  std::string prev_;
};

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
