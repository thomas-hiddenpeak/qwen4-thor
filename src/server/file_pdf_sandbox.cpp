#include "q4t/server/file_pdf.h"

#include <fcntl.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/landlock.h>
#include <linux/seccomp.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <cerrno>
#include <cstddef>
#include <vector>

namespace q4t::server::detail {
namespace {
bool AddPath(int rules, int fd, uint64_t access) {
  const landlock_path_beneath_attr rule{access, fd};
  return syscall(SYS_landlock_add_rule, rules, LANDLOCK_RULE_PATH_BENEATH,
                 &rule, 0) == 0;
}
bool ReadPath(int rules, const char* path, bool executable = false) {
  // All paths here are fixed system resources, never client metadata. Follow
  // distribution /lib symlinks, then validate the actual trusted object.
  const int fd = open(path, O_PATH | O_CLOEXEC);
  if (fd < 0) return errno == ENOENT && !executable;
  struct stat info {};
  bool ok = fstat(fd, &info) == 0 && info.st_uid == 0 &&
            !(info.st_mode & (S_IWGRP | S_IWOTH)) &&
            (S_ISREG(info.st_mode) || S_ISDIR(info.st_mode));
  const uint64_t access =
      LANDLOCK_ACCESS_FS_READ_FILE |
      (S_ISDIR(info.st_mode) ? LANDLOCK_ACCESS_FS_READ_DIR : 0) |
      (executable ? LANDLOCK_ACCESS_FS_EXECUTE : 0);
  if (ok) ok = AddPath(rules, fd, access);
  close(fd);
  return ok;
}
bool Filesystem(int executable_fd) {
  const int abi = syscall(SYS_landlock_create_ruleset, nullptr, 0,
                          LANDLOCK_CREATE_RULESET_VERSION);
  if (abi < 4 || executable_fd != 4) return false;
  const landlock_ruleset_attr policy{
      // ABI 3 adds TRUNCATE; ABI 4 adds TCP controls. Later capabilities
      // are not presumed present on this device. Seccomp denies all sockets.
      (LANDLOCK_ACCESS_FS_TRUNCATE << 1) - 1,
      LANDLOCK_ACCESS_NET_BIND_TCP | LANDLOCK_ACCESS_NET_CONNECT_TCP};
  const int rules =
      syscall(SYS_landlock_create_ruleset, &policy, sizeof(policy), 0);
  if (rules < 0) return false;
  // The kernel opens an ELF executable for reading as well as execution.
  // Grant both on this exact inode; never grant execute on a library tree.
  bool ok = AddPath(rules, executable_fd,
                    LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE);
  for (const char* path : {"/usr/lib", "/lib", "/etc/ld.so.cache", "/etc/fonts",
                           "/usr/share/fonts", "/usr/share/fontconfig",
                           "/usr/share/poppler", "/var/cache/fontconfig"})
    ok = ok && ReadPath(rules, path);
#if defined(__aarch64__)
  ok = ok && ReadPath(rules, "/lib/ld-linux-aarch64.so.1", true);
#elif defined(__x86_64__)
  ok = ok && ReadPath(rules, "/lib64/ld-linux-x86-64.so.2", true);
#else
  ok = false;
#endif
  ok = ok && prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0 &&
       syscall(SYS_landlock_restrict_self, rules, 0) == 0;
  close(rules);
  return ok;
}
}  // namespace

bool EnterPdfSandbox(int executable_fd) {
  if (!Filesystem(executable_fd)) return false;
#if defined(__aarch64__)
  constexpr uint32_t architecture = AUDIT_ARCH_AARCH64;
#elif defined(__x86_64__)
  constexpr uint32_t architecture = AUDIT_ARCH_X86_64;
#else
  return false;
  constexpr uint32_t architecture = 0;
#endif
  constexpr uint32_t denied = SECCOMP_RET_ERRNO | EPERM;
  std::vector<sock_filter> filter = {
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, arch)),
      BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, architecture, 1, 0),
      BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr))};
  // Initial exec has only the fixed, already-opened trusted binary. Its
  // descriptor is CLOEXEC. The filesystem policy persists across exec.
  filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_execveat, 0, 6));
  filter.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, args[0])));
  filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, 4, 0, 3));
  filter.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, args[4])));
  filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AT_EMPTY_PATH, 0, 1));
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, denied));
  filter.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)));
  // Read-only opens plus Landlock: no host state mutation or client paths.
  filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_openat, 0, 4));
  filter.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, args[2])));
  filter.push_back(BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K,
                            O_WRONLY | O_RDWR | O_CREAT | O_TRUNC | O_APPEND |
                                (O_TMPFILE & ~O_DIRECTORY),
                            0, 1));
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, denied));
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
  filter.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)));
  for (uint32_t call : {SYS_read,
                        SYS_write,
                        SYS_readv,
                        SYS_writev,
                        SYS_pread64,
                        SYS_lseek,
                        SYS_close,
                        SYS_recvfrom,
                        SYS_sendto,
                        SYS_dup3,
                        SYS_fstat,
                        SYS_newfstatat,
                        SYS_statx,
                        SYS_readlinkat,
                        SYS_faccessat,
                        SYS_faccessat2,
                        SYS_getdents64,
                        SYS_mmap,
                        SYS_mprotect,
                        SYS_munmap,
                        SYS_mremap,
                        SYS_madvise,
                        SYS_brk,
                        SYS_futex,
                        SYS_clock_gettime,
                        SYS_getpid,
                        SYS_getppid,
                        SYS_gettid,
                        SYS_getuid,
                        SYS_geteuid,
                        SYS_getgid,
                        SYS_getegid,
                        SYS_getrandom,
                        SYS_getcwd,
                        SYS_uname,
                        SYS_sched_getaffinity,
                        SYS_set_tid_address,
                        SYS_set_robust_list,
                        SYS_rseq,
                        SYS_rt_sigreturn,
                        SYS_rt_sigprocmask,
                        SYS_rt_sigaction,
                        SYS_sigaltstack,
                        SYS_exit,
                        SYS_exit_group}) {
    filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, call, 0, 1));
    filter.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
  }
#if defined(__x86_64__)
  for (uint32_t call : {SYS_arch_prctl, SYS_dup2, SYS_readlink, SYS_access}) {
    filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, call, 0, 1));
    filter.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
  }
#endif
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, denied));
  sock_fprog program{static_cast<unsigned short>(filter.size()), filter.data()};
  return syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, 0, &program) == 0;
}
}  // namespace q4t::server::detail
