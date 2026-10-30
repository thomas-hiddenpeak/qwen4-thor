"""Exercise the production supervisor loop with controlled clocks and children."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
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
                for target, name in [(release.subprocess, 'run'),
                                     (release.threading, 'Thread'),
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


if __name__ == '__main__':
    unittest.main()
