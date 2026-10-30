"""Observe production supervisor cleanup using only owned CPU subprocesses."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path('/home/rm01/models/dev/qwen4-thor')
OUT = ROOT / '.q4t-work/main-reaudit-20261031'
REAL_POPEN = subprocess.Popen
REAL_EVENT = threading.Event
assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
child_code = '''import json,os,signal,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid()}))
while True: time.sleep(1)
'''
leader_code = '''import subprocess,sys,time
subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]])
while True: time.sleep(1)
'''


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


results = []
for label in ('current', 'parent'):
    if label == 'current':
        source = ROOT / 'tools/release/release.py'
    else:
        source = OUT / 'parent-release.py'
        source.write_bytes(subprocess.check_output([
            'git', 'show', 'c26e97a:tools/release/release.py'], cwd=ROOT))
    release = load(source, 'audit_release_' + label)
    root = OUT / ('supervisor-' + label)
    root.mkdir()
    package = root / 'package'
    package.mkdir()
    (root / 'current').symlink_to(package)
    for name in ('data', 'control'):
        (package / (name + '.conf.in')).write_text('synthetic config')
    (root / 'config.json').write_text(json.dumps(dict(
        backend_port=1, data_port=2, control_port=3,
        nginx='synthetic-nginx', nginx_sha256='synthetic')))
    marker = root / 'owned-grandchild.json'
    spawned = []
    grandchild = None
    event_calls = 0

    class Event:
        def set(self):
            pass

        def wait(self, seconds):
            return True

    def make_event():
        global event_calls
        event_calls += 1
        return Event() if event_calls == 1 else REAL_EVENT()

    def popen(command, **kwargs):
        is_backend = command[0].endswith('/q4t')
        if is_backend:
            actual = [sys.executable, '-c', leader_code, child_code, str(marker)]
        else:
            actual = [sys.executable, '-c', 'import time; time.sleep(60)']
        process = REAL_POPEN(actual, **kwargs)
        spawned.append(process)
        if is_backend:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert marker.exists(), 'owned child readiness timeout'
        return process

    started = time.monotonic()
    try:
        # Real children, capture threads, killpg, wait, identity and production
        # finally block. Only GPU/nginx launch commands and config checks differ.
        with patch.object(release, 'verify', lambda _: {}), \
             patch.object(release, 'sha', lambda _: 'synthetic'), \
             patch.object(release.subprocess, 'run'), \
             patch.object(release.subprocess, 'Popen', popen), \
             patch.object(release.threading, 'Event', make_event), \
             patch.object(release.signal, 'signal'):
            release.supervise(root)
        grandchild = json.loads(marker.read_text())['pid']
        start = release.identity(grandchild)
        result = dict(source=label, supervisor_returned=True,
            supervisor_marker_exists=(root / 'supervisor.json').exists(),
            leaders_reaped=all(p.poll() is not None for p in spawned),
            surviving_owned_grandchild=grandchild if start is not None else None,
            elapsed_seconds=time.monotonic() - started)
        results.append(result)
    finally:
        if marker.exists():
            grandchild = json.loads(marker.read_text())['pid']
            try:
                fd = os.pidfd_open(grandchild)
                try:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
            except ProcessLookupError:
                pass
        for process in spawned:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
        if grandchild:
            os.waitpid(grandchild, 0)
    results[-1]['probe_cleanup_reaped_owned_descendant'] = True
    print(json.dumps(results[-1]))
(OUT / 'supervisor-observations.json').write_text(json.dumps(results, indent=2) + '\n')
