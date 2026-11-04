"""Exercise the production supervisor loop with controlled clocks and children."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import release


class SupervisorProgressTest(unittest.TestCase):
    def run_loop(self, snapshots, missing=False):
        output = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR',
                                      release.ROOT / 'build/supervisor-tests'))
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output) as temporary:
            root = Path(temporary)
            package = root / 'package'
            package.mkdir()
            (root / 'current').symlink_to(package)
            for name in ['data', 'control']:
                (package / (name + '.conf.in')).write_text('synthetic config')
            (root / 'config.json').write_text(json.dumps(dict(
                backend_port=1, data_port=2, control_port=3,
                nginx='synthetic-nginx', nginx_sha256='synthetic')))
            clock = types.SimpleNamespace(now=0, index=-1)
            kills = []

            class Event:
                def set(self):
                    pass

                def wait(self, seconds):
                    clock.index += 1
                    if clock.index == len(snapshots):
                        return True
                    clock.now = snapshots[clock.index][0]
                    return False

            class Child:
                def __init__(self, command, **kwargs):
                    self.name = ('backend' if command[0].endswith('/q4t')
                                 else 'proxy')
                    self.pid = 10000000
                    self.stdout = None

                def poll(self):
                    return None

                def kill(self):
                    kills.append((self.name, clock.now))

                def terminate(self):
                    pass

                def wait(self, **kwargs):
                    return 0

            def response(port, route):
                if route == '/healthz':
                    return 200, '{}'
                _, progress, received, done = snapshots[clock.index]
                # Completed-output counters stay zero during a long response.
                text = (f'q4t_requests_total {received}\n'
                        f'q4t_requests_aborted_total {done}\n'
                        'q4t_generation_tokens_total 0\n')
                if not missing:
                    text += f'q4t_execution_steps_total {progress}\n'
                return 200, text

            with ExitStack() as stack:
                for target, name, value in [
                    (release, 'verify', lambda _: {}),
                    (release, 'sha', lambda _: 'synthetic'),
                    (release, 'identity', lambda _: 'synthetic'),
                    (release, 'request', response),
                    (release.subprocess, 'Popen', Child),
                    (release.threading, 'Event', Event),
                    (release.time, 'monotonic', lambda: clock.now),
                ]:
                    stack.enter_context(patch.object(target, name, value))
                thread_mock = stack.enter_context(
                    patch.object(release.threading, 'Thread'))
                thread_mock.return_value.is_alive.return_value = False
                for target, name in [(release.subprocess, 'run'),
                                     (release.signal, 'signal'),
                                     (release.os, 'killpg')]:
                    stack.enter_context(patch.object(target, name))
                release.supervise(root)
            return kills

    def test_long_request_advances_before_completion(self):
        self.assertEqual(self.run_loop([
            (0, 1, 1, 0), (600, 2, 1, 0), (1200, 3, 1, 0),
            (1800, 4, 1, 0), (2400, 5, 1, 0)]), [])

    def test_stalled_execution_expires(self):
        self.assertEqual(self.run_loop([
            (0, 1, 1, 0), (1260, 1, 1, 0), (1261, 1, 1, 0)]),
            [('backend', 1261)])

    def test_queued_cancellations_cannot_mask_stall(self):
        self.assertEqual(self.run_loop([
            (0, 1, 1, 0), (600, 1, 2, 1), (1261, 1, 3, 2)]),
            [('backend', 1261)])

    def test_idle_time_does_not_charge_next_request(self):
        self.assertEqual(self.run_loop([
            (0, 0, 0, 0), (2000, 0, 0, 0), (2001, 0, 1, 0),
            (2500, 1, 1, 0)]), [])

    def test_missing_progress_metric_rejects_incompatible_backend(self):
        with self.assertRaisesRegex(RuntimeError, 'q4t_execution_steps_total'):
            self.run_loop([(0, 0, 1, 0)], missing=True)


class SupervisorCleanupTest(unittest.TestCase):
    def run_owned(self, leader_exits=False, launch_failure=False,
                  cleanup_failure=False, restart=False):
        output = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR',
                                      release.ROOT / 'build/supervisor-tests'))
        output.mkdir(parents=True, exist_ok=True)
        real_popen, real_thread, real_event = (
            subprocess.Popen, threading.Thread, threading.Event)
        original_stop, original_signal = release.stop_children, release.signal_member
        child_code = '''import json,os,signal,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid()}))
while True: time.sleep(1)
'''
        leader_code = '''import subprocess,sys,time
from pathlib import Path
subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]])
while not Path(sys.argv[2]).exists(): time.sleep(.005)
if sys.argv[3] == 'exit': sys.exit(0)
while True: time.sleep(1)
'''
        with tempfile.TemporaryDirectory(dir=output) as temporary:
            root = Path(temporary)
            package = root/'package'
            package.mkdir()
            (root/'current').symlink_to(package)
            for name in ['data', 'control']:
                (package/(name+'.conf.in')).write_text('synthetic config')
            (root/'config.json').write_text(json.dumps(dict(
                backend_port=1, data_port=2, control_port=3,
                nginx='synthetic-nginx', nginx_sha256='synthetic')))
            spawned, descendants, readers = [], [], []
            backend_starts, event_calls, iterations = 0, 0, 0

            def popen(command, **kwargs):
                nonlocal backend_starts
                backend = command[0].endswith('/q4t')
                if launch_failure and not backend:
                    raise RuntimeError('injected proxy launch failure')
                if backend:
                    if backend_starts:
                        self.assertTrue(all(release.identity(pid) is None
                                            for pid, _ in descendants))
                    marker = root/f'grandchild-{backend_starts}.json'
                    exits = (leader_exits or restart) and backend_starts == 0
                    actual = [sys.executable, '-c', leader_code, child_code,
                              str(marker), 'exit' if exits else 'wait']
                    backend_starts += 1
                else:
                    actual = [sys.executable, '-c', 'import time; time.sleep(60)']
                child = real_popen(actual, **kwargs)
                spawned.append(child)
                if backend:
                    deadline = time.monotonic()+5
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(marker.exists(), 'child readiness')
                    pid = json.loads(marker.read_text())['pid']
                    descendants.append((pid, release.identity(pid)))
                    if exits:
                        child.wait(timeout=5)
                return child

            class StopEvent:
                def set(self):
                    pass

                def wait(self, seconds):
                    nonlocal iterations
                    if not restart:
                        return True
                    if seconds == 2:
                        return False
                    iterations += 1
                    return iterations > 1

            def event():
                nonlocal event_calls
                event_calls += 1
                return StopEvent() if event_calls == 1 else real_event()

            def thread(*args, **kwargs):
                reader = real_thread(*args, **kwargs)
                readers.append(reader)
                return reader

            def short_stop(children, logs, grace=.05, kill_grace=2):
                return original_stop(children, logs, grace, kill_grace)

            def send(pid, start, sig):
                if cleanup_failure and sig == signal.SIGKILL:
                    raise PermissionError('injected kill denial')
                return original_signal(pid, start, sig)

            def response(port, route):
                return 200, ('{}' if route == '/healthz' else
                             'q4t_execution_steps_total 0\n')

            try:
                with ExitStack() as stack:
                    for target, name, value in [
                        (release, 'verify', lambda _: {}),
                        (release, 'sha', lambda _: 'synthetic'),
                        (release, 'request', response),
                        (release, 'stop_children', short_stop),
                        (release, 'signal_member', send),
                        (release.subprocess, 'Popen', popen),
                        (release.threading, 'Event', event),
                        (release.threading, 'Thread', thread),
                    ]:
                        stack.enter_context(patch.object(target, name, value))
                    stack.enter_context(patch.object(release.subprocess, 'run'))
                    stack.enter_context(patch.object(release.signal, 'signal'))
                    if launch_failure:
                        with self.assertRaisesRegex(RuntimeError, 'proxy launch'):
                            release.supervise(root)
                    elif cleanup_failure:
                        with self.assertRaisesRegex(PermissionError, 'kill denial'):
                            release.supervise(root)
                    else:
                        release.supervise(root)
                if cleanup_failure:
                    self.assertTrue((root/'supervisor.json').exists())
                    self.assertTrue((root/'cleanup-failure.json').exists())
                    self.assertNotIn('supervisor stopped',
                                     (root/'events.log').read_text())
                    # Simulate the supervisor's eventual exit. The durable
                    # marker still prevents a replacement process tree.
                    with patch.object(release, 'running', lambda _: None):
                        with self.assertRaisesRegex(RuntimeError, 'cleanup'):
                            release.start(root)
                        with self.assertRaisesRegex(RuntimeError, 'cleanup'):
                            release.stop(root)
                    with self.assertRaisesRegex(RuntimeError, 'cleanup'):
                        release.supervise(root)
                else:
                    self.assertFalse((root/'supervisor.json').exists())
                    self.assertFalse((root/'cleanup-failure.json').exists())
                    self.assertTrue(all(child.poll() is not None for child in spawned))
                    self.assertTrue(all(release.identity(pid) is None
                                        for pid, _ in descendants))
                    self.assertTrue(all(not reader.is_alive() for reader in readers))
                    self.assertIn('supervisor stopped', (root/'events.log').read_text())
                    self.assertEqual(backend_starts, 2 if restart else 1)
            finally:
                # Only exact identities created by this test; never a blanket
                # process-name kill, even on an assertion/production failure.
                for pid, start in descendants:
                    original_signal(pid, start, signal.SIGKILL)
                for child in spawned:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)
                for pid, _ in descendants:
                    try:
                        os.waitpid(pid, 0)
                    except ChildProcessError:
                        pass
                for reader in readers:
                    reader.join(timeout=5)

    def test_stop_reaps_term_ignoring_grandchild(self):
        self.run_owned()

    def test_stop_reaps_grandchild_after_leader_already_exited(self):
        self.run_owned(leader_exits=True)

    def test_partial_startup_failure_reaps_entire_started_group(self):
        self.run_owned(launch_failure=True)

    def test_restart_waits_for_old_group_to_be_empty(self):
        self.run_owned(restart=True)

    def test_cleanup_failure_retains_state_and_blocks_replacement(self):
        self.run_owned(cleanup_failure=True)


if __name__ == '__main__':
    unittest.main()
