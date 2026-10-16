// Diagnostic owner of three VMM backing allocations. Launch a complete stage
// driver with FDs 73..75; keep handles alive until that driver exits. Run only
// under gpu_exclusive.py. No model, kernel, throughput measurement or memset.
#include <cuda.h>
#include <fcntl.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstdlib>

extern char** environ;
namespace {
[[noreturn]] void Die(const char* message) {
  std::fprintf(stderr, "[persistent-owner] INVALID %s\n", message);
  std::exit(1);
}
void Check(CUresult result) {
  if (result != CUDA_SUCCESS) {
    std::fprintf(stderr, "[persistent-owner] cuda_error=%d\n", result);
    Die("CUDA operation failed");
  }
}
void Posix(int error) {
  if (error) {
    std::fprintf(stderr, "[persistent-owner] posix_error=%d\n", error);
    Die("POSIX operation failed");
  }
}
}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) Die("stage driver command required");
  Check(cuInit(0));
  CUdevice device;
  CUcontext context;
  Check(cuDeviceGet(&device, 0));
  Check(cuDevicePrimaryCtxRetain(&context, device));
  Check(cuCtxSetCurrent(context));
  CUmemAllocationProp prop{};
  prop.type = CU_MEM_ALLOCATION_TYPE_PINNED;
  prop.location.type = CU_MEM_LOCATION_TYPE_DEVICE;
  prop.location.id = device;
  prop.requestedHandleTypes = CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR;
  size_t granularity;
  Check(cuMemGetAllocationGranularity(&granularity, &prop,
                                    CU_MEM_ALLOC_GRANULARITY_MINIMUM));
  if (granularity != 2097152) Die("granularity changed");
  constexpr size_t sizes[] = {167772160, 167772160, 2348023808};
  CUmemGenericAllocationHandle handles[3]{};
  int fds[3]{};
  posix_spawn_file_actions_t actions;
  Posix(posix_spawn_file_actions_init(&actions));
  for (int i = 0; i < 3; ++i) {
    const size_t bytes = (sizes[i] + granularity - 1) / granularity * granularity;
    Check(cuMemCreate(&handles[i], bytes, &prop, 0));
    Check(cuMemExportToShareableHandle(
        &fds[i], handles[i], CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR, 0));
    // Never silently overwrite another inherited FD or clobber a source FD
    // during ordered spawn dup2 actions.
    if (fds[i] >= 73) Die("unexpected export FD range");
    errno = 0;
    if (fcntl(73 + i, F_GETFD) >= 0 || errno != EBADF)
      Die("reserved child FD already in use");
    const int flags = fcntl(fds[i], F_GETFD);
    if (flags < 0 || fcntl(fds[i], F_SETFD, flags | FD_CLOEXEC) < 0)
      Die("export CLOEXEC failed");
    Posix(posix_spawn_file_actions_adddup2(&actions, fds[i], 73 + i));
    std::fprintf(stderr,
                 "[persistent-owner] slot=%d handle=%llu bytes=%zu child_fd=%d\n",
                 i, handles[i], bytes, 73 + i);
  }
  pid_t child;
  Posix(posix_spawnp(&child, argv[1], &actions, nullptr, argv + 1, environ));
  Posix(posix_spawn_file_actions_destroy(&actions));
  std::fprintf(stderr, "[persistent-owner] driver_pid=%d\n", child);
  int status;
  pid_t waited;
  do {
    waited = waitpid(child, &status, 0);
  } while (waited < 0 && errno == EINTR);
  if (waited != child) Die("driver wait failed");
  for (int i = 2; i >= 0; --i) {
    if (close(fds[i])) Die("export close failed");
    Check(cuMemRelease(handles[i]));
  }
  Check(cuDevicePrimaryCtxRelease(device));
  const int result = WIFEXITED(status) ? WEXITSTATUS(status)
                                      : 128 + WTERMSIG(status);
  std::fprintf(stderr,
               "[persistent-owner] cleanup_complete=3 driver_exit=%d\n", result);
  return result;
}
