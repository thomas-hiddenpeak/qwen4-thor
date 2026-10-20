"""Run one payload as a scope descendant and retain final cgroup counters."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value) + '\n')
    temporary.replace(path)


def main():
    folder = Path(sys.argv[1])
    spec = json.loads((folder / 'spec.json').read_text())
    child = None
    interrupted = []

    def forward(signum, _frame):
        interrupted.append(signum)
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)
    with Path(spec['log_path']).open('a') as log:
        child = subprocess.Popen(spec['command'], cwd=spec['cwd'], env=spec['env'],
                                 stdout=log, stderr=subprocess.STDOUT)
        # A signal arriving between Popen and publication must not be lost.
        for signum in interrupted:
            if child.poll() is None:
                child.send_signal(signum)
        stat = Path(f'/proc/{child.pid}/stat').read_text().rsplit(')', 1)[1].split()
        save(folder / 'payload.json', dict(pid=child.pid, start_ticks=int(stat[19]),
             helper_pid=os.getpid(), token=spec['token'],
             membership=Path(f'/proc/{child.pid}/cgroup').read_text().splitlines()))
        rc = child.wait()
    save(folder / 'payload-exit.json', dict(returncode=rc, token=spec['token'],
                                          finished=time.time()))
    # The controller snapshots counters and stops this exact unit. Bounded
    # retention also drains naturally if the controller itself disappears.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not interrupted:
        time.sleep(.1)
    return 0


if __name__ == '__main__':
    sys.exit(main())
