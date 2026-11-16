#include "file_video_protocol.h"

#include <dirent.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/sched.h>
#include <linux/seccomp.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <cerrno>
#include <cstddef>
#include <vector>

namespace q4t::server::video_detail {
int EnterVideoSandbox() {
  // NEW_LISTENER cannot be combined with TSYNC. Refuse pre-existing library
  // threads; every later pthread must inherit the filter and be metered.
  DIR* tasks = opendir("/proc/self/task");
  if (!tasks) return -1;
  size_t threads = 0;
  while (const auto* task = readdir(tasks))
    if (task->d_name[0] != '.') ++threads;
  closedir(tasks);
  if (threads != 1) return -1;
#if defined(__aarch64__)
  constexpr uint32_t arch = AUDIT_ARCH_AARCH64;
#elif defined(__x86_64__)
  constexpr uint32_t arch = AUDIT_ARCH_X86_64;
#else
  constexpr uint32_t arch = 0;
  return -1;
#endif
  constexpr uint32_t denied = SECCOMP_RET_ERRNO | EPERM;
  std::vector<sock_filter> f = {
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, arch)),
      BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, arch, 1, 0),
      BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr))};
  for (uint32_t call : {SYS_mmap, SYS_mprotect}) {
    f.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, call, 0, 4));
    f.push_back(
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, args[2])));
    f.push_back(BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, PROT_EXEC, 0, 1));
    f.push_back(BPF_STMT(BPF_RET | BPF_K, denied));
    f.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
    f.push_back(BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)));
  }
  // Permit pthreads only, never child processes/namespaces. Returning ENOSYS
  // for clone3 makes glibc use the inspectable legacy clone flags.
  f.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone3, 0, 1));
  f.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | ENOSYS));
  constexpr uint32_t required = CLONE_VM | CLONE_SIGHAND | CLONE_THREAD;
  constexpr uint32_t allowed =
      required | CLONE_FS | CLONE_FILES | CLONE_SYSVSEM | CLONE_SETTLS |
      CLONE_PARENT_SETTID | CLONE_CHILD_CLEARTID | CLONE_CHILD_SETTID;
  f.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone, 0, 8));
  f.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, args[0])));
  f.push_back(BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, ~allowed, 0, 1));
  f.push_back(BPF_STMT(BPF_RET | BPF_K, denied));
  f.push_back(BPF_STMT(BPF_ALU | BPF_AND | BPF_K, required));
  f.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, required, 1, 0));
  f.push_back(BPF_STMT(BPF_RET | BPF_K, denied));
  f.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_USER_NOTIF));
  f.push_back(BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)));
  // The only ancillary FD transfer is the listener to the authenticated core.
  f.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_sendmsg, 0, 4));
  f.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, args[0])));
  f.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, 3, 0, 1));
  f.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
  f.push_back(BPF_STMT(BPF_RET | BPF_K, denied));
  f.push_back(BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)));
  for (uint32_t call :
       {SYS_read,           SYS_write,         SYS_close,
        SYS_recvfrom,       SYS_sendto,        SYS_brk,
        SYS_munmap,         SYS_mremap,        SYS_madvise,
        SYS_futex,          SYS_clock_gettime, SYS_getpid,
        SYS_gettid,         SYS_getrandom,     SYS_rt_sigreturn,
        SYS_rt_sigprocmask, SYS_rt_sigaction,  SYS_sigaltstack,
        SYS_exit,           SYS_exit_group,    SYS_set_robust_list,
        SYS_rseq,           SYS_sched_yield,   SYS_sched_getaffinity,
        SYS_eventfd2,       SYS_ppoll,         SYS_fstat,
        SYS_lseek,          SYS_getuid,        SYS_geteuid,
        SYS_getgid,         SYS_getegid}) {
    f.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, call, 0, 1));
    f.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
  }
#ifdef SYS_poll
  f.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_poll, 0, 1));
  f.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
#endif
  f.push_back(BPF_STMT(BPF_RET | BPF_K, denied));
  sock_fprog program{static_cast<unsigned short>(f.size()), f.data()};
  if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) return -1;
  return syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER,
                 SECCOMP_FILTER_FLAG_NEW_LISTENER, &program);
}
}  // namespace q4t::server::video_detail
