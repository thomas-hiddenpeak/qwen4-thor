// Faults exist only in the separately built validation executable.
#pragma once

#ifdef Q4T_SERVE_FAULT_INJECTION
#include <atomic>
#include <cstdlib>
#include <cstring>
#include <new>
#endif

namespace q4t::server::detail {
#ifdef Q4T_SERVE_FAULT_INJECTION
inline void (*serve_test_hook)(const char*) = nullptr;
#endif
inline void ServeFaultPoint(const char* name) {
#ifdef Q4T_SERVE_FAULT_INJECTION
  if (serve_test_hook) serve_test_hook(name);
  static std::atomic<bool> fired{false};
  const char* selected = std::getenv("Q4T_SERVE_FAULT");
  if (selected && std::strcmp(selected, name) == 0 && !fired.exchange(true))
    throw std::bad_alloc();
#else
  (void)name;
#endif
}
}  // namespace q4t::server::detail
