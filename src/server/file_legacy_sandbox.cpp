#include "q4t/server/file_legacy.h"

#include <fcntl.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/landlock.h>
#include <linux/seccomp.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <ucontext.h>
#include <unistd.h>

#include <cerrno>
#include <cstddef>
#include <csignal>
#include <vector>

namespace q4t::server::detail {
namespace {
// Codec read-only MAP_SHARED views become private views in this child only.
// A subsequent mprotect(PROT_WRITE) therefore cannot write back to the file.
// This handler is not an isolation boundary: direct shared writable syscalls
// remain denied even if untrusted code replaces the handler.
void PrivateReadView(int signal, siginfo_t* info, void* context) {
  if (signal != SIGSYS || !info || info->si_code != 1 ||
      info->si_syscall != SYS_mmap)
    _exit(127);
  auto* state = static_cast<ucontext_t*>(context);
  const int saved_errno = errno;
#if defined(__aarch64__)
  auto* args = state->uc_mcontext.regs;
  const auto flags = (args[3] & ~MAP_TYPE) | MAP_PRIVATE;
  const long result =
      syscall(SYS_mmap, args[0], args[1], args[2], flags, args[4], args[5]);
  args[0] = result == -1 ? -errno : result;
#elif defined(__x86_64__)
  auto* args = state->uc_mcontext.gregs;
  const auto flags = (args[REG_R10] & ~MAP_TYPE) | MAP_PRIVATE;
  const long result = syscall(SYS_mmap, args[REG_RDI], args[REG_RSI],
                              args[REG_RDX], flags, args[REG_R8], args[REG_R9]);
  args[REG_RAX] = result == -1 ? -errno : result;
#else
  _exit(127);
#endif
  errno = saved_errno;
}
bool Grant(int rules, int fd, uint64_t access) {
  const landlock_path_beneath_attr rule{access, fd};
  return syscall(SYS_landlock_add_rule, rules, LANDLOCK_RULE_PATH_BENEATH,
                 &rule, 0) == 0;
}
bool SystemPath(int rules, const char* path, bool writable = false) {
  const int fd = open(path, O_PATH | O_CLOEXEC);
  if (fd < 0) return errno == ENOENT;
  struct stat info {};
  bool ok = fstat(fd, &info) == 0 && info.st_uid == 0 &&
            (S_ISREG(info.st_mode) || S_ISDIR(info.st_mode) ||
             (writable && S_ISCHR(info.st_mode))) &&
            (writable || !(info.st_mode & (S_IWGRP | S_IWOTH)));
  if (ok)
    ok = Grant(rules, fd,
               LANDLOCK_ACCESS_FS_READ_FILE |
                   (S_ISDIR(info.st_mode) ? LANDLOCK_ACCESS_FS_READ_DIR : 0) |
                   (writable ? LANDLOCK_ACCESS_FS_WRITE_FILE : 0));
  close(fd);
  return ok;
}
bool Filesystem(int scratch) {
  if (syscall(SYS_landlock_create_ruleset, nullptr, 0,
              LANDLOCK_CREATE_RULESET_VERSION) < 4)
    return false;
  struct stat info {};
  if (fstat(scratch, &info) || !S_ISDIR(info.st_mode) ||
      info.st_uid != getuid() || (info.st_mode & 077))
    return false;
  const landlock_ruleset_attr policy{
      (LANDLOCK_ACCESS_FS_TRUNCATE << 1) - 1,
      LANDLOCK_ACCESS_NET_BIND_TCP | LANDLOCK_ACCESS_NET_CONNECT_TCP};
  const int rules =
      syscall(SYS_landlock_create_ruleset, &policy, sizeof(policy), 0);
  if (rules < 0) return false;
  // No execution, device nodes, sockets, symlinks or hard links in scratch.
  bool ok =
      Grant(rules, scratch,
            LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR |
                LANDLOCK_ACCESS_FS_WRITE_FILE | LANDLOCK_ACCESS_FS_TRUNCATE |
                LANDLOCK_ACCESS_FS_MAKE_REG | LANDLOCK_ACCESS_FS_MAKE_DIR |
                LANDLOCK_ACCESS_FS_REMOVE_FILE | LANDLOCK_ACCESS_FS_REMOVE_DIR |
                LANDLOCK_ACCESS_FS_REFER);
  for (const char* path :
       {"/usr/lib", "/lib", "/etc/ld.so.cache", "/etc/fonts",
        "/usr/share/fonts", "/var/cache/fontconfig", "/usr/share/fontconfig",
        "/usr/share/libreoffice", "/etc/libreoffice", "/usr/share/locale",
        "/usr/share/mime", "/usr/share/zoneinfo", "/etc/localtime",
        "/etc/passwd", "/etc/group", "/etc/nsswitch.conf", "/proc/meminfo",
        "/sys/devices/system/cpu"})
    ok = ok && SystemPath(rules, path);
  ok = ok && SystemPath(rules, "/dev/null", true);
  // Proc views expose this worker only. No host process trees or descriptors.
  for (const char* leaf : {"statm", "status", "maps", "auxv"}) {
    const auto path = "/proc/" + std::to_string(getpid()) + "/" + leaf;
    const int fd = open(path.c_str(), O_PATH | O_CLOEXEC);
    if (fd < 0) {
      ok = false;
      break;
    }
    ok = ok && Grant(rules, fd, LANDLOCK_ACCESS_FS_READ_FILE);
    close(fd);
  }
  ok = ok && prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0 &&
       syscall(SYS_landlock_restrict_self, rules, 0) == 0;
  close(rules);
  return ok;
}
}  // namespace

int EnterLegacyDocumentSandbox(int scratch_fd) {
  if (!Filesystem(scratch_fd)) return -1;
  struct sigaction action {};
  action.sa_sigaction = PrivateReadView;
  action.sa_flags = SA_SIGINFO;
  sigemptyset(&action.sa_mask);
  if (sigaction(SIGSYS, &action, nullptr)) return -1;
#if defined(__aarch64__)
  constexpr uint32_t architecture = AUDIT_ARCH_AARCH64;
#elif defined(__x86_64__)
  constexpr uint32_t architecture = AUDIT_ARCH_X86_64;
#else
  return -1;
  constexpr uint32_t architecture = 0;
#endif
  constexpr uint32_t deny = SECCOMP_RET_ERRNO | EPERM;
  std::vector<sock_filter> filter{
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, arch)),
      BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, architecture, 1, 0),
      BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr))};
  // Filesystem writes must not bypass cumulative scalar-argument admission.
  // In particular: no writev/pwritev, io_uring, splice, copy_file_range,
  // fallocate, shared mmap, or ioctl clone/reflink paths.
  std::vector<uint32_t> metered_calls{SYS_write,  SYS_pwrite64, SYS_ftruncate,
                                      SYS_openat, SYS_mkdirat,  SYS_clone};
#if defined(__x86_64__)
  // x86 libc still uses the original pathname ABI for directory creation.
  // Charge the same budget as *at calls; Landlock checks the actual path.
  metered_calls.insert(metered_calls.end(), {SYS_open, SYS_mkdir});
#endif
  for (uint32_t call : metered_calls) {
    filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, call, 0, 1));
    filter.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_USER_NOTIF));
  }
  // libc can fall back to the bounded scalar clone ABI for pthread creation.
  filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone3, 0, 1));
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | ENOSYS));
  filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_mmap, 0, 7));
  filter.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, args[3])));
  filter.push_back(BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, MAP_SHARED, 0, 4));
  filter.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, args[2])));
  filter.push_back(BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, PROT_WRITE, 0, 1));
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, deny));
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_TRAP));
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
  filter.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)));
  // Own-process thread cancellation only; no signal delivery to the core.
  filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_tgkill, 0, 4));
  filter.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, args[0])));
  filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K,
                            static_cast<uint32_t>(getpid()), 0, 1));
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, deny));
  filter.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)));
  // Local wakeup pairs have no pathname/network access and use the FD limit.
  filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_socketpair, 0, 4));
  filter.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, args[0])));
  filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AF_UNIX, 0, 1));
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, deny));
  filter.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)));
  // Only the authenticated control socket can transfer ancillary descriptors.
  filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_sendmsg, 0, 4));
  filter.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, args[0])));
  filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, 3, 0, 1));
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, deny));
  filter.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)));
  // fcntl must not arrange asynchronous signals to another process.
  const std::vector<uint32_t> fcntl_commands{
      F_GETFD, F_SETFD, F_GETFL, F_SETFL, F_DUPFD_CLOEXEC, F_GETLK, F_SETLK};
  filter.push_back(
      BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_fcntl, 0,
               static_cast<uint8_t>(2 + 2 * fcntl_commands.size())));
  filter.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, args[1])));
  for (uint32_t command : fcntl_commands) {
    filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, command, 0, 1));
    filter.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
  }
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, deny));
  filter.push_back(
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)));
  for (uint32_t call : {SYS_read,
                        SYS_readv,
                        SYS_pread64,
                        SYS_lseek,
                        SYS_close,
                        SYS_recvfrom,
                        SYS_sendto,
                        SYS_dup3,
                        SYS_fstat,
                        SYS_newfstatat,
                        SYS_statx,
                        SYS_statfs,
                        SYS_fstatfs,
                        SYS_readlinkat,
                        SYS_faccessat,
                        SYS_faccessat2,
                        SYS_getdents64,
                        SYS_renameat,
                        SYS_renameat2,
                        SYS_unlinkat,
                        SYS_chdir,
                        SYS_fchdir,
                        SYS_fsync,
                        SYS_fdatasync,
                        SYS_mprotect,
                        SYS_munmap,
                        SYS_mremap,
                        SYS_madvise,
                        SYS_brk,
                        SYS_futex,
                        SYS_clock_gettime,
                        SYS_clock_nanosleep,
                        SYS_nanosleep,
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
                        SYS_sysinfo,
                        SYS_sched_yield,
                        SYS_sched_getaffinity,
                        SYS_set_tid_address,
                        SYS_set_robust_list,
                        SYS_rseq,
                        SYS_rt_sigreturn,
                        SYS_rt_sigprocmask,
                        SYS_rt_sigaction,
                        SYS_sigaltstack,
                        SYS_ppoll,
                        SYS_pipe2,
                        SYS_eventfd2,
                        SYS_exit,
                        SYS_exit_group}) {
    filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, call, 0, 1));
    filter.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
  }
#if defined(__x86_64__)
  // These have the same Landlock restrictions as the permitted *at forms.
  for (uint32_t call :
       {SYS_arch_prctl, SYS_dup2, SYS_readlink, SYS_access, SYS_poll, SYS_stat,
        SYS_lstat, SYS_rename, SYS_unlink, SYS_rmdir}) {
    filter.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, call, 0, 1));
    filter.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
  }
#endif
  filter.push_back(BPF_STMT(BPF_RET | BPF_K, deny));
  sock_fprog program{static_cast<unsigned short>(filter.size()), filter.data()};
  return syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER,
                 SECCOMP_FILTER_FLAG_NEW_LISTENER, &program);
}
}  // namespace q4t::server::detail
