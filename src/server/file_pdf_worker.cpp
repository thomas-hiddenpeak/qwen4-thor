#include "q4t/server/file_pdf.h"

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <vector>

#include "file_pdf_protocol.h"
#include "q4t/server/lifetime.h"

namespace q4t::server {
namespace {
using namespace pdf_detail;
bool Reply(Ready ready) {
  return detail::WorkerTransfer(&ready, sizeof(ready), true);
}
int Run(int argc, char** argv) {
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  const rlimit cpu{120, 120};
  if (setrlimit(RLIMIT_CPU, &cpu)) return 2;
  Request request;
  if (!detail::WorkerTransfer(&request, sizeof(request), false) ||
      request.magic != kMagic || !request.bytes ||
      request.bytes > kFileInputBytes ||
      (request.operation != Operation::kInfo &&
       request.operation != Operation::kText &&
       request.operation != Operation::kPage) ||
      (request.operation == Operation::kPage &&
       (!request.page || request.page > 256 ||
        (request.edge != 512 && request.edge != 1536 && request.edge != 2048))))
    return 3;
  int source = memfd_create("q4t-pdf", MFD_CLOEXEC | MFD_ALLOW_SEALING);
  if (source < 0) return Reply(Ready::kDependency) ? 0 : 3;
  ScopeExit close_source([&] {
    if (source >= 0) close(source);
  });
  std::array<char, 65536> buffer;
  for (uint64_t left = request.bytes; left;) {
    size_t size = std::min<uint64_t>(left, buffer.size());
    if (!detail::WorkerTransfer(buffer.data(), size, false)) return 3;
    size_t position = 0;
    while (position < size) {
      const auto written =
          write(source, buffer.data() + position, size - position);
      if (written < 0 && errno == EINTR) continue;
      if (written <= 0) return Reply(Ready::kInput) ? 0 : 3;
      position += written;
    }
    left -= size;
  }
  if (lseek(source, 0, SEEK_SET) != 0 ||
      fcntl(source, F_ADD_SEALS,
            F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL) ||
      dup2(source, STDIN_FILENO) != STDIN_FILENO)
    return Reply(Ready::kInput) ? 0 : 3;
  close(source);
  source = -1;
  // No filename, password, environment or command supplied by the request.
  const char* binary = "/usr/bin/pdfinfo";
  std::vector<std::string> args;
  if (request.operation == Operation::kText) {
    binary = "/usr/bin/pdftotext";
    args = {binary, "-enc", "UTF-8", "-layout", "-nopgbrk", "-", "-"};
  } else if (request.operation == Operation::kPage) {
    binary = "/usr/bin/pdftoppm";
    const auto page = std::to_string(request.page);
    // Omitting an output prefix uses stdout. A literal '-' prefix would
    // instead create '-.png'; filesystem writes are forbidden in either case.
    args = {binary, "-png",      "-singlefile",
            "-f",   page,        "-l",
            page,   "-scale-to", std::to_string(request.edge),
            "-"};
  } else {
    args = {binary, "-enc", "UTF-8", "-"};
  }
  const int executable = open(binary, O_PATH | O_NOFOLLOW | O_CLOEXEC);
  if (executable < 0) return Reply(Ready::kDependency) ? 0 : 3;
  ScopeExit close_executable([&] { close(executable); });
  struct stat info {};
  if (executable != 4 || fstat(executable, &info) || !S_ISREG(info.st_mode) ||
      info.st_uid != 0 ||
      (info.st_mode & (S_IWGRP | S_IWOTH | S_ISUID | S_ISGID)) ||
      !(info.st_mode & S_IXUSR))
    return Reply(Ready::kDependency) ? 0 : 3;
  std::vector<char*> arguments;
  for (auto& arg : args) arguments.push_back(arg.data());
  arguments.push_back(nullptr);
  char locale[] = "LC_ALL=C";
  char arenas[] = "MALLOC_ARENA_MAX=1";
  char fonts[] = "FONTCONFIG_FILE=/etc/fonts/fonts.conf";
  char* environment[] = {locale, arenas, fonts, nullptr};
  const rlimit file_size{0, 0};
  if (chdir("/") || setrlimit(RLIMIT_FSIZE, &file_size) ||
      !detail::EnterPdfSandbox(executable))
    return Reply(Ready::kSandbox) ? 0 : 3;
  if (!Reply(Ready::kReady) || dup2(3, STDOUT_FILENO) != STDOUT_FILENO)
    return 3;
  close(3);
  // Same owned PID, pidfd, parent-death signal, deadline and rlimits. No fork,
  // shell or process tree; the sandbox and no_new_privs persist across exec.
  syscall(SYS_execveat, executable, "", arguments.data(), environment,
          AT_EMPTY_PATH);
  return 127;
}
}  // namespace

int MaybeRunFilePdfWorker(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--file-pdf-worker")) return -1;
  try {
    return Run(argc, argv);
  } catch (...) {
    return 3;
  }
}
}  // namespace q4t::server
