#include "q4t/server/serve_diagnostics.h"

#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <csignal>
#include <exception>
#include <thread>
#include <utility>

#include "q4t/service/guardian_process.h"

namespace q4t::server {
namespace {
using service::OwnedFd;

void Forward(OwnedFd reader, OwnedFd sink) {
  char bytes[4096];
  for (;;) {
    const ssize_t size = read(reader.Get(), bytes, sizeof(bytes));
    if (size < 0 && errno == EINTR) continue;
    if (size <= 0) return;
    if (sink.Get() < 0) continue;
    ssize_t done = 0;
    while (done < size) {
      const ssize_t n = write(sink.Get(), bytes + done, size - done);
      if (n < 0 && errno == EINTR) continue;
      if (n < 0 && errno == EPIPE) sink.Reset();
      if (n <= 0) break;
      done += n;
    }
  }
}

bool StartForwarding() {
  OwnedFd sink(fcntl(STDERR_FILENO, F_DUPFD_CLOEXEC, 3));
  if (sink.Get() < 0) return false;
  int pair[2];
  if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, pair) != 0)
    return false;
  OwnedFd reader(pair[0]), writer(pair[1]);
  // stdin or stdout may have been closed before exec. Keep the worker's
  // endpoint private if socketpair temporarily allocated one of those slots.
  if (reader.Get() < 3) {
    reader.Reset(fcntl(reader.Get(), F_DUPFD_CLOEXEC, 3));
    if (reader.Get() < 0) return false;
  }
  const int flags = fcntl(writer.Get(), F_GETFL);
  if (flags < 0 || fcntl(writer.Get(), F_SETFL, flags | O_NONBLOCK) != 0)
    return false;
  try {
    std::thread(Forward, std::move(reader), std::move(sink)).detach();
  } catch (const std::exception&) {
    return false;
  }
  // The inherited sink may be shared with the parent or sibling writers.
  // Only the private producer endpoint has been made nonblocking.
  return dup2(writer.Get(), STDERR_FILENO) == STDERR_FILENO;
}

void DiscardDiagnostics() {
  OwnedFd null(open("/dev/null", O_WRONLY | O_CLOEXEC));
  if (null.Get() == STDERR_FILENO) {
    null.Release();
  } else if (null.Get() < 0 || dup2(null.Get(), STDERR_FILENO) < 0) {
    close(STDERR_FILENO);
  }
}
}  // namespace

ServeDiagnostics::ServeDiagnostics() {
  std::signal(SIGPIPE, SIG_IGN);
  forwarding_ = StartForwarding();
  if (!forwarding_) DiscardDiagnostics();
}

ServeDiagnostics::~ServeDiagnostics() {
  if (!forwarding_) return;
  // An exec'd helper may also have inherited stderr. Shut down our end
  // explicitly so a healthy collector can flush even if a helper survives.
  shutdown(STDERR_FILENO, SHUT_WR);
  pollfd drained{STDERR_FILENO, 0, 0};
  poll(&drained, 1, 150);
}
}  // namespace q4t::server
