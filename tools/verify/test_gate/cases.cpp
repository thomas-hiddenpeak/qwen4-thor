// Synthetic runner fixtures. Never part of the real acceptance test registry.
#include "q4t/test.h"
#include <stdexcept>

namespace {
bool cleaned = false;
struct Guard {
  ~Guard() { cleaned = true; }
};
const bool many = [] {
  for (int i = 0; i < 260; ++i)
    q4t::test::Register("many_" + std::to_string(i), [] { return false; });
  return true;
}();
}  // namespace

Q4T_TEST(ok) { return true; }
Q4T_TEST(bad) { return false; }
Q4T_TEST(skip_cleanup) {
  Guard guard;
  Q4T_SKIP("synthetic missing prerequisite");
}
Q4T_TEST(cleanup_observed) { return cleaned; }
Q4T_TEST(exception) { throw std::runtime_error("synthetic failure"); }
