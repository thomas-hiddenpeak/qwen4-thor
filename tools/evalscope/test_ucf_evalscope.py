"""CPU-only collector protocol tests; no sudo, hardware events or model."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from ucf_evalscope import CounterWindow

FAKE = r'''
import json, sys, time
mode = sys.argv[1]
def emit(value): print(json.dumps(value), flush=True)
emit({'event': 'ready'})
assert input() == 'start'
start = time.monotonic_ns()
emit({'event': 'started', 'begin_ns': start, 'end_ns': start})
try: command = input()
except EOFError: sys.exit(2)
assert command == 'stop'
stop = time.monotonic_ns()
emit({'event': 'stopped', 'begin_ns': stop, 'end_ns': stop})
if mode == 'missing': sys.exit(0)
emit({'event': 'snapshot', 'valid': True, 'enabled_ns': stop-start,
      'running_ns': stop-start if mode != 'multiplex' else 1,
      'events': [{'config': c, 'id': i if mode != 'badids' else 0,
                  'value': 100} for i,c in enumerate([4294967296,0,6,12])]})
'''


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def window(self, mode='ok'):
        window = CounterWindow([sys.executable, '-u', '-c', FAKE, mode],
                               Path(self.temp.name) / mode, timeout=2)
        self.addCleanup(window.close)
        return window

    def test_success_preserves_raw_and_clock_bounds(self):
        window = self.window()
        window.start()
        start = time.perf_counter()
        window.first(start, time.perf_counter())
        self.assertTrue(window.complete)
        saved = json.loads((window.evidence / 'counter.json').read_text())
        self.assertEqual(len(saved['rows']), 4)
        self.assertGreaterEqual(saved['boundary']['first_to_stop_ns'], 0)
        self.assertFalse(saved['performance_acceptance'])

    def test_incomplete_successful_process_rejected(self):
        window = self.window('missing')
        window.start()
        with self.assertRaisesRegex(RuntimeError, 'EOF'):
            window.first(time.perf_counter(), time.perf_counter())
        self.assertFalse(window.complete)

    def test_multiplexed_snapshot_rejected(self):
        window = self.window('multiplex')
        window.start()
        with self.assertRaisesRegex(RuntimeError, 'multiplexed'):
            window.first(time.perf_counter(), time.perf_counter())
        self.assertFalse(window.complete)

    def test_duplicate_ids_rejected(self):
        window = self.window('badids')
        window.start()
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            window.first(time.perf_counter(), time.perf_counter())

    def test_no_first_output_closed_as_invalid(self):
        window = self.window()
        window.start()
        window.close('no first output')
        saved = json.loads((window.evidence / 'counter.json').read_text())
        self.assertFalse(saved['complete'])
        self.assertEqual(saved['error'], 'no first output')
        self.assertEqual(window.process.returncode, 2)

    def test_second_start_rejected(self):
        window = self.window()
        window.start()
        with self.assertRaisesRegex(RuntimeError, 'one request'):
            window.start()


class IntegratedTests(unittest.TestCase):
    setUp = WindowTests.setUp
    window = WindowTests.window
    def test_real_evalscope_boundary_to_fake_collector(self):
        from types import SimpleNamespace
        from evalscope.perf.plugin.api.default_api import DefaultApiPlugin
        from test_ucf_client_boundary import Session, message
        from ucf_evalscope import install
        window = self.window()
        original = install(window)
        self.addCleanup(setattr, DefaultApiPlugin, 'process_request', original)
        result = asyncio.run(DefaultApiPlugin.process_request(
            SimpleNamespace(), Session([message({'role': 'assistant'}),
                                        message({'content': 'A'}),
                                        b'data: [DONE]\n\n'], []),
            'http://fake', {}, {'stream': True}))
        self.assertTrue(result.success)
        self.assertTrue(window.complete)
        self.assertAlmostEqual(result.first_chunk_latency * 1e9,
                               window.boundary['first_output_ns'] -
                               window.boundary['request_start_ns'], delta=1)

    def test_empty_response_cannot_leave_valid_observation(self):
        from types import SimpleNamespace
        from evalscope.perf.plugin.api.default_api import DefaultApiPlugin
        from test_ucf_client_boundary import Session, message
        from ucf_evalscope import install
        window = self.window()
        original = install(window)
        self.addCleanup(setattr, DefaultApiPlugin, 'process_request', original)
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            asyncio.run(DefaultApiPlugin.process_request(
                SimpleNamespace(), Session([message({'role': 'assistant'}),
                                            b'data: [DONE]\n\n'], []),
                'http://fake', {}, {'stream': True}))
        self.assertFalse(window.complete)
        self.assertEqual(window.process.returncode, 2)


if __name__ == '__main__':
    unittest.main()
