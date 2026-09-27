"""Exercise the exact production connection predicate over real loopback TCP."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
ROOT = Path(__file__).resolve().parents[3]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
out = a.output.resolve()
assert any(out.is_relative_to(ROOT / x) for x in ['build', '.q4t-work'])
out.mkdir(parents=True, exist_ok=False)
shutil.copy2(__file__, out)
s = (ROOT / 'src/server/chat_server.cpp').read_text()
start = s.index('bool ClientConnectionFailed(')
function = s[start:s.index('\n}\n', start) + 3]
source = '''#include <arpa/inet.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>
#include <cassert>
#include <chrono>
#include <thread>
''' + function + '''
int main() {
  for (int mode = 0; mode < 3; ++mode) {
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    assert(listener >= 0);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    assert(bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0);
    socklen_t length = sizeof(address);
    assert(getsockname(listener, reinterpret_cast<sockaddr*>(&address), &length) == 0);
    assert(listen(listener, 1) == 0);
    int client = socket(AF_INET, SOCK_STREAM, 0);
    assert(connect(client, reinterpret_cast<sockaddr*>(&address), length) == 0);
    int server = accept(listener, nullptr, nullptr);
    assert(server >= 0);
    if (mode == 1) {
      assert(shutdown(client, SHUT_WR) == 0);
      pollfd incoming{server, POLLIN, 0};
      assert(poll(&incoming, 1, 1000) == 1);
    }
    if (mode == 2) {
      linger reset{1, 0};
      assert(setsockopt(client, SOL_SOCKET, SO_LINGER, &reset, sizeof(reset)) == 0);
      close(client);
      for (int i = 0; i < 1000 && !ClientConnectionFailed(server); ++i)
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
      assert(ClientConnectionFailed(server));
    } else {
      assert(!ClientConnectionFailed(server));
      assert(send(server, "ok", 2, MSG_NOSIGNAL) == 2);
      char data[2];
      assert(recv(client, data, 2, MSG_WAITALL) == 2);
      assert(data[0] == 'o' && data[1] == 'k');
      close(client);
    }
    close(server);
    close(listener);
  }
}
'''
(out / 'check.cpp').write_text(source)
command = ['g++-14', '-std=c++23', '-Wall', '-Wextra', str(out / 'check.cpp'), '-o', str(out / 'check')]
(out / 'command.json').write_text(json.dumps(command))
with (out / 'build.log').open('w') as log:
    subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
assert 'warning' not in (out / 'build.log').read_text().lower()
r = subprocess.run([str(out / 'check')])
(out / 'exit.json').write_text(json.dumps({'returncode': r.returncode, 'cases': ['live', 'write-half-close', 'reset']}))
raise SystemExit(r.returncode)
