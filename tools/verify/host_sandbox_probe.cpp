// Diagnose host prerequisites without changing the parent or bypassing tests.
#include <fcntl.h>
#include <linux/landlock.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <string>

#include "q4t/server/file_document_worker.h"
#include "q4t/server/file_legacy.h"
#include "q4t/server/file_pdf.h"

namespace {
struct Result {
  int ready = 0;
  int error = 0;
};
bool Probe(const char* name, const std::string& scratch) {
  int channel[2];
  if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, channel))
    return false;
  // fd 4 is the PDF worker's fixed executable descriptor.
  const int writer = fcntl(channel[1], F_DUPFD_CLOEXEC, 10);
  close(channel[1]);
  if (writer < 0) {
    close(channel[0]);
    return false;
  }
  std::fflush(stdout);
  const pid_t child = fork();
  if (child == 0) {
    close(channel[0]);
    Result result;
    if (std::string(name) == "pdf") {
      const int executable = open("/proc/self/exe", O_PATH | O_CLOEXEC);
      if (executable >= 0 && dup2(executable, 4) == 4) {
        if (executable != 4) close(executable);
        result.ready = q4t::server::detail::EnterPdfSandbox(4);
      }
    } else if (std::string(name) == "legacy") {
      const int directory = open(scratch.c_str(), O_PATH | O_CLOEXEC);
      if (directory >= 0)
        result.ready =
            q4t::server::detail::EnterLegacyDocumentSandbox(directory) >= 0;
    } else {
      result.ready = q4t::server::detail::EnterNativeDocumentSandbox();
    }
    result.error = result.ready ? 0 : errno;
    const auto written = send(writer, &result, sizeof(result), MSG_NOSIGNAL);
    _exit(written == sizeof(result) ? 0 : 2);
  }
  close(writer);
  Result result;
  ssize_t length = -1;
  if (child > 0) length = read(channel[0], &result, sizeof(result));
  close(channel[0]);
  int status = 0;
  pid_t waited = -1;
  if (child > 0) {
    do {
      waited = waitpid(child, &status, 0);
    } while (waited < 0 && errno == EINTR);
  }
  const bool reported = length == sizeof(result) && waited == child &&
                        WIFEXITED(status) && WEXITSTATUS(status) == 0;
  std::printf("sandbox=%s reported=%d ready=%d errno=%d status=%d\n", name,
              reported, result.ready, result.error, status);
  return reported;
}
}  // namespace

int main(int argc, char** argv) {
  if (argc != 2) return 2;
  utsname system{};
  if (uname(&system)) return 2;
  errno = 0;
  const long abi = syscall(SYS_landlock_create_ruleset, nullptr, 0,
                           LANDLOCK_CREATE_RULESET_VERSION);
  std::printf("kernel=%s arch=%s landlock_abi=%ld errno=%d\n", system.release,
              system.machine, abi, abi < 0 ? errno : 0);
  for (const char* path : {"/usr/lib", "/lib", "/etc/ld.so.cache", "/etc/fonts",
                           "/usr/share/fonts", "/var/cache/fontconfig",
                           "/usr/share/fontconfig", "/usr/share/poppler",
                           "/lib64/ld-linux-x86-64.so.2",
                           "/lib/ld-linux-aarch64.so.1"}) {
    struct stat info {};
    const int status = stat(path, &info);
    std::printf("path=%s uid=%u mode=%o errno=%d\n", path, info.st_uid,
                info.st_mode, status < 0 ? errno : 0);
  }
  std::filesystem::create_directories(argv[1]);
  auto scratch = (std::filesystem::path(argv[1]) / "sandbox-XXXXXX").string();
  if (!mkdtemp(scratch.data())) return 2;
  bool reported = true;
  for (const char* name : {"native", "pdf", "legacy"})
    reported = Probe(name, scratch) && reported;
  std::filesystem::remove(scratch);
  // This is a diagnostic receipt. Actual isolation remains a required gate
  // in q4t_public_host_contracts, including negative filesystem/network tests.
  return reported ? 0 : 2;
}
