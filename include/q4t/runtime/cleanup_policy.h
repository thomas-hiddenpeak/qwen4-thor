#pragma once

#include <cstdio>
#include <cstdlib>

namespace q4t::runtime {

// Fixed labels and thread-local state need no heap storage during teardown.
inline thread_local const char* cleanup_phase = "unscoped";
inline thread_local bool handled_device_release = false;

class CleanupPhase {
 public:
  explicit CleanupPhase(const char* phase) noexcept : previous_(cleanup_phase) {
    cleanup_phase = phase;
  }
  ~CleanupPhase() { cleanup_phase = previous_; }
  CleanupPhase(const CleanupPhase&) = delete;
  CleanupPhase& operator=(const CleanupPhase&) = delete;

 private:
  const char* previous_;
};

// Only the serving adapter with an explicit error/stop/drain owner uses this.
// It permits cudaFree to return its error, not other cleanup or wait APIs.
class HandledDeviceRelease {
 public:
  HandledDeviceRelease() noexcept : previous_(handled_device_release) {
    handled_device_release = true;
  }
  ~HandledDeviceRelease() { handled_device_release = previous_; }
  HandledDeviceRelease(const HandledDeviceRelease&) = delete;
  HandledDeviceRelease& operator=(const HandledDeviceRelease&) = delete;

 private:
  bool previous_;
};

inline int RequireCleanupSuccess(int error, const char* operation,
                                 bool device_release = false) noexcept {
  if (error == 0 || (device_release && handled_device_release)) return error;
  // Unknown completion/ownership cannot safely unwind or retry release. The
  // process exits without running destructors; the OS/driver reclaims it.
  // A fatal failure may truncate an active HTTP response, never forge success.
  std::fprintf(stderr, "[q4t][cleanup-fatal] api=%s phase=%s error=%d\n",
               operation, cleanup_phase, error);
  std::_Exit(1);
}

}  // namespace q4t::runtime
