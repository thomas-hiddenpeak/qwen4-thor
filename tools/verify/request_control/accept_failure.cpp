// Test-only fatal accept failure after a completed connection acceptance.
#include <dlfcn.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstdlib>

extern "C" int accept(int fd, sockaddr* address, socklen_t* length) {
  using Accept = int (*)(int, sockaddr*, socklen_t*);
  static const auto real = reinterpret_cast<Accept>(dlsym(RTLD_NEXT, "accept"));
  if (!real) std::_Exit(127);
  const int client = real(fd, address, length);
  const char* flag = std::getenv("Q4T_TEST_ACCEPT_FAIL_FLAG");
  if (client >= 0 && flag && access(flag, F_OK) == 0) {
    close(client);
    std::fprintf(stderr, "[accept-fault] injected EMFILE\n");
    errno = EMFILE;
    return -1;
  }
  return client;
}
