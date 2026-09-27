// Minimal test framework for q4t.
//
// Tests register themselves via a static initializer and are run by
// RunAllTests(). No external dependency; keeps the build self-contained.
// A test returns true on success, false on failure.
#pragma once

#include <cstdio>
#include <cstdlib>
#include <exception>
#include <functional>
#include <unordered_set>
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

struct SkipTest {
  std::string reason;
};

[[noreturn]] inline void Skip(const std::string& reason) {
  throw SkipTest{reason};
}

// Validate selection before running anything. A missing prerequisite is never
// a pass; strict callers require every selected check to actually complete.
inline int RunTests(const std::vector<std::string>& names,
                    bool allow_skips = false) {
  if (names.empty()) {
    std::fprintf(stderr, "No tests selected\n");
    return 2;
  }
  std::unordered_set<std::string> registered, selected;
  for (const auto& c : Registry()) {
    if (!registered.insert(c.name).second) {
      std::fprintf(stderr, "Duplicate test registration: %s\n", c.name.c_str());
      return 2;
    }
  }
  for (const auto& name : names) {
    if (!registered.contains(name) || !selected.insert(name).second) {
      std::fprintf(stderr, "Unknown or duplicate selected test: %s\n",
                   name.c_str());
      return 2;
    }
  }
  int passed = 0, failed = 0, skipped = 0;
  for (const auto& c : Registry()) {
    if (!selected.contains(c.name)) continue;
    try {
      const bool ok = c.fn();
      std::printf("[%s] %s\n", ok ? "PASS" : "FAIL", c.name.c_str());
      ok ? ++passed : ++failed;
    } catch (const SkipTest& skip) {
      ++skipped;
      std::printf("[SKIP] %s: %s\n", c.name.c_str(), skip.reason.c_str());
    } catch (const std::exception& error) {
      ++failed;
      std::printf("[FAIL] %s: %s\n", c.name.c_str(), error.what());
    } catch (...) {
      ++failed;
      std::printf("[FAIL] %s: unknown exception\n", c.name.c_str());
    }
  }
  std::printf("%zu tests, %d passed, %d failed, %d skipped\n",
              names.size(), passed, failed, skipped);
  return failed || (skipped && !allow_skips) ? 1 : 0;
}

inline int RunAllTests() {
  std::vector<std::string> names;
  for (const auto& c : Registry()) names.push_back(c.name);
  return RunTests(names);
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

#define Q4T_SKIP(reason) ::q4t::test::Skip(reason)
