#include "q4t/runtime/cleanup_policy.h"

#include <sys/wait.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <thread>

#include "q4t/test.h"

namespace {
using q4t::runtime::CleanupPhase;
using q4t::runtime::HandledDeviceRelease;
using q4t::runtime::RequireCleanupSuccess;

bool FatalChild(bool handled, bool release) {
  const pid_t child = fork();
  if (child == 0) {
    struct MustNotUnwind {
      ~MustNotUnwind() { std::_Exit(42); }
    } owner;
    std::atexit([] { std::_Exit(43); });
    const CleanupPhase phase("host.fatal");
    if (handled) {
      const HandledDeviceRelease scope;
      RequireCleanupSuccess(719, "synchronize", release);
    } else {
      RequireCleanupSuccess(719, "release", release);
    }
    std::_Exit(44);
  }
  if (child < 0) return false;
  int status = 0;
  pid_t result;
  do {
    result = waitpid(child, &status, 0);
  } while (result < 0 && errno == EINTR);
  return result == child && WIFEXITED(status) && WEXITSTATUS(status) == 1;
}
}  // namespace

Q4T_TEST(cleanup_policy_scope_is_nested_and_thread_local) {
  {
    const CleanupPhase phase("outer");
    const HandledDeviceRelease handled;
    bool isolated = false;
    std::thread worker([&] {
      isolated = !q4t::runtime::handled_device_release &&
                 !std::strcmp(q4t::runtime::cleanup_phase, "unscoped");
      const CleanupPhase inner("worker");
    });
    worker.join();
    Q4T_CHECK(isolated);
    {
      const CleanupPhase inner("nested");
      const HandledDeviceRelease nested;
    }
    Q4T_CHECK(q4t::runtime::handled_device_release &&
              !std::strcmp(q4t::runtime::cleanup_phase, "outer"));
  }
  Q4T_CHECK(!q4t::runtime::handled_device_release &&
            !std::strcmp(q4t::runtime::cleanup_phase, "unscoped"));
  return true;
}

Q4T_TEST(cleanup_policy_handled_release_returns_original_error) {
  Q4T_CHECK(RequireCleanupSuccess(0, "normal") == 0);
  const HandledDeviceRelease handled;
  for (int error : {1, 2, 700, 719, 999})
    Q4T_CHECK(RequireCleanupSuccess(error, "cudaFree", true) == error);
  return true;
}

Q4T_TEST(cleanup_policy_unknown_cleanup_exits_without_unwind_or_atexit) {
  Q4T_CHECK(FatalChild(false, true));
  Q4T_CHECK(FatalChild(false, false));
  return true;
}

Q4T_TEST(cleanup_policy_handled_free_cannot_suppress_failed_drain) {
  Q4T_CHECK(FatalChild(true, false));
  return true;
}
