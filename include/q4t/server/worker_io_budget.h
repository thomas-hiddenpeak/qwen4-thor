// Kernel-notified resource admission for an owned isolated codec process.
#pragma once

#include <cstdint>

namespace q4t::server {
struct WorkerIoBudget {
  uint64_t write_bytes = 256ULL << 20;
  uint64_t file_creations = 2048;
  uint64_t thread_creations = 64;
  uint64_t notifications = 1000000;
};
namespace detail {
// Send one seccomp listener to the already authenticated parent on fd 3.
// The caller must close its copy before invoking any untrusted decoder.
bool SendWorkerIoListener(int listener);
}  // namespace detail
}  // namespace q4t::server
