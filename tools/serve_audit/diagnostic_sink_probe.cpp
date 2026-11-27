// Drive the production diagnostic forwarder without loading a model.
#include <fcntl.h>
#include <sys/resource.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstring>

#include "q4t/server/serve_diagnostics.h"

namespace {
int LimitedDescriptors(const char* path) {
  rlimit previous{};
  if (getrlimit(RLIMIT_NOFILE, &previous) != 0) return 1;
  rlimit limited = previous;
  limited.rlim_cur = 3;
  if (setrlimit(RLIMIT_NOFILE, &limited) != 0) return 1;
  q4t::server::ServeDiagnostics diagnostics;
  if (setrlimit(RLIMIT_NOFILE, &previous) != 0) return 1;
  const int fd = open(path, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
  if (fd < 0) return 1;
  std::fprintf(stderr, "diagnostic after descriptor exhaustion\n");
  std::printf("%d\n", fd);
  return close(fd) == 0 ? 0 : 1;
}
}  // namespace

int main(int argc, char** argv) {
  if (argc == 3 && std::strcmp(argv[1], "--limited-fds") == 0)
    return LimitedDescriptors(argv[2]);
  q4t::server::ServeDiagnostics diagnostics;
  setvbuf(stdout, nullptr, _IONBF, 0);
  std::puts("ready");
  char command[32];
  while (std::fgets(command, sizeof(command), stdin)) {
    if (std::strcmp(command, "burst\n") == 0) {
      char bytes[4096] = {};
      int dropped = 0;
      for (int i = 0; i < 8192; ++i) {
        if (write(STDERR_FILENO, bytes, sizeof(bytes)) < 0 && errno == EAGAIN)
          ++dropped;
      }
      std::printf("%d\n", dropped);
    } else if (std::strcmp(command, "marker\n") == 0) {
      std::fprintf(stderr, "diagnostic-after-resume\n");
      std::puts("wrote");
    } else {
      break;
    }
  }
  return 0;
}
