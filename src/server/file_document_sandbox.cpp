#include "q4t/server/file_document_worker.h"

#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <cerrno>
#include <cstddef>
#include <vector>

namespace q4t::server::detail {
bool EnterNativeDocumentSandbox() {
#if defined(__aarch64__)
  constexpr uint32_t architecture = AUDIT_ARCH_AARCH64;
#elif defined(__x86_64__)
  constexpr uint32_t architecture = AUDIT_ARCH_X86_64;
#else
  constexpr uint32_t architecture = 0;
  return false;
#endif
  constexpr uint32_t denied = SECCOMP_RET_ERRNO | EPERM;
  std::vector<sock_filter> filter = {
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, arch)),
      BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, architecture, 1, 0),
      BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr))};
  // No executable allocation; all required code has already been loaded.
  for (uint32_t call : {SYS_mmap, SYS_mprotect}) {
    filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, call, 0, 4));
    filter.push_back(
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, args[2])));
    filter.push_back(BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, PROT_EXEC, 0, 1));
    filter.push_back(BPF_STMT(BPF_RET | BPF_K, denied));
    filter.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
    filter.push_back(
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)));
  }
  for (uint32_t call : {SYS_read,           SYS_write,         SYS_close,
                        SYS_recvfrom,       SYS_sendto,        SYS_brk,
                        SYS_munmap,         SYS_mremap,        SYS_madvise,
                        SYS_futex,          SYS_clock_gettime, SYS_getpid,
                        SYS_gettid,         SYS_getrandom,     SYS_rt_sigreturn,
                        SYS_rt_sigprocmask, SYS_rt_sigaction,  SYS_sigaltstack,
                        SYS_exit,           SYS_exit_group}) {
    filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, call, 0, 1));
    filter.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
  }
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, denied));
  sock_fprog program{static_cast<unsigned short>(filter.size()), filter.data()};
  return prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0 &&
         syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, 0, &program) == 0;
}
}  // namespace q4t::server::detail
