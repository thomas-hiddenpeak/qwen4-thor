// Drive the production diagnostic forwarder without loading a model.
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstring>

#include "q4t/server/serve_diagnostics.h"

int main() {
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
