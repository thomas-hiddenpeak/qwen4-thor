// Faults exist only in the separately built validation executable.
#pragma once

#include <cstdint>
#include <span>

#ifdef Q4T_SERVE_FAULT_INJECTION
#include <atomic>
#include <cstdlib>
#include <cstring>
#include <new>
#include <chrono>
#include <cstdio>
#include <fcntl.h>
#include <stdexcept>
#include <thread>
#include <unistd.h>
#endif

namespace q4t::server::detail {
#ifdef Q4T_SERVE_FAULT_INJECTION
inline void (*serve_test_hook)(const char*) = nullptr;
#endif
inline void ServeFaultPoint(const char* name) {
#ifdef Q4T_SERVE_FAULT_INJECTION
  if (serve_test_hook) serve_test_hook(name);
  static std::atomic<bool> observed{false}, paused{false};
  const char* observe = std::getenv("Q4T_SERVE_OBSERVE");
  if (observe && std::strcmp(observe, name) == 0 && !observed.exchange(true))
    std::fprintf(stderr, "[serve-test] observed=%s\n", name);
  const char* pause = std::getenv("Q4T_SERVE_PAUSE");
  if (pause && std::strcmp(pause, name) == 0 && !paused.exchange(true)) {
    const char* gate = std::getenv("Q4T_SERVE_GATE");
    if (!gate) throw std::runtime_error("missing validation gate");
    char path[4096];
    if (std::snprintf(path, sizeof(path), "%s.entered", gate) >=
        static_cast<int>(sizeof(path)))
      throw std::runtime_error("validation gate path too long");
    const int fd = ::open(path, O_CREAT | O_EXCL | O_WRONLY, 0600);
    if (fd < 0) throw std::runtime_error("validation gate creation failed");
    ::close(fd);
    std::snprintf(path, sizeof(path), "%s.release", gate);
    const auto end = std::chrono::steady_clock::now() +
                     std::chrono::seconds(10);
    while (::access(path, F_OK) != 0) {
      if (std::chrono::steady_clock::now() >= end)
        throw std::runtime_error("validation gate timed out");
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
  }
  static std::atomic<bool> fired{false};
  const char* selected = std::getenv("Q4T_SERVE_FAULT");
  if (selected && std::strcmp(selected, name) == 0 && !fired.exchange(true))
    throw std::bad_alloc();
#else
  (void)name;
#endif
}

inline void ServeRequestFaultPoint(const char* name, const char* request) {
#ifdef Q4T_SERVE_FAULT_INJECTION
  const char* selected = std::getenv("Q4T_SERVE_REQUEST");
  if (!selected || std::strcmp(selected, request) == 0) ServeFaultPoint(name);
#else
  (void)name;
  (void)request;
#endif
}

inline void ServeSnapshotTokens(const char* request,
                                std::span<const int32_t> tokens) {
#ifdef Q4T_SERVE_FAULT_INJECTION
  if (!std::getenv("Q4T_SERVE_PREFIX_IDS")) return;
  flockfile(stderr);
  std::fprintf(stderr, "[serve-test-prefix] request=%s tokens=[", request);
  for (size_t i = 0; i < tokens.size(); ++i)
    std::fprintf(stderr, "%s%d", i ? "," : "", tokens[i]);
  std::fprintf(stderr, "]\n");
  funlockfile(stderr);
#else
  (void)request;
  (void)tokens;
#endif
}
}  // namespace q4t::server::detail
