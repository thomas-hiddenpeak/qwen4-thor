"""CPU-only OC window failure contracts; no model or sysfs writes."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import types
import run_acceptance
from oc_evalscope import OcWindow


class WindowTests(unittest.TestCase):
    def window(self, change=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        calls = 0
        def read():
            nonlocal calls
            calls += 1
            row = dict(begin_ns=time.monotonic_ns(), end_ns=time.monotonic_ns(),
                       boot_id='boot', device='device',
                       values={f'oc{i}_{key}': value for i in (1, 2, 3)
                               for key, value in [('event_cnt', 10), ('throt_en', 1)]})
            if calls == 2:
                row['values']['oc3_event_cnt'] = 13
                if change:
                    change(row)
            return row
        return OcWindow(Path(temp.name) / 'evidence', read)

    def test_success_records_delta_and_slack(self):
        w = self.window(); w.start()
        w.first(time.perf_counter(), time.perf_counter())
        self.assertTrue(w.complete)
        self.assertEqual(w.delta, {'oc1': 0, 'oc2': 0, 'oc3': 3})
        r = json.loads((w.evidence / 'counter.json').read_text())
        self.assertFalse(r['performance_acceptance'])
        self.assertGreaterEqual(r['boundary']['trailing_slack_ns'], 0)

    def test_invalid_identity_enable_decrease(self):
        changes = [lambda r: r.update(boot_id='other'),
                   lambda r: r.update(device='other'),
                   lambda r: r['values'].update(oc3_throt_en=0),
                   lambda r: r['values'].update(oc3_event_cnt=9)]
        for change in changes:
            with self.subTest(change=change):
                w = self.window(change); w.start()
                with self.assertRaises(RuntimeError):
                    w.first(time.perf_counter(), time.perf_counter())
                w.close('invalid')
                self.assertFalse(w.complete)
                self.assertEqual(len(w.rows), 2)

    def test_missing_first_never_complete(self):
        w = self.window(); w.start(); w.close()
        self.assertFalse(w.complete)

    def test_duplicate_start_or_first_rejected(self):
        w = self.window(); w.start()
        with self.assertRaises(RuntimeError): w.start()
        w.first(time.perf_counter(), time.perf_counter())
        with self.assertRaises(RuntimeError):
            w.first(time.perf_counter(), time.perf_counter())

    def test_clock_order_rejected(self):
        w = self.window(); w.start()
        with self.assertRaises(RuntimeError): w.first(0.0, 0.0)
        self.assertFalse(w.complete)

    def test_reader_failure_not_success(self):
        w = self.window()
        def bad(): raise OSError('missing counter')
        w.reader = bad
        with self.assertRaises(OSError): w.start()
        w.close('missing counter')
        self.assertFalse(w.complete)
        self.assertEqual(w.rows, [])


class RunnerTests(unittest.TestCase):
    def exercise(self, counter, streaming=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); case = root / 'context-204800'; case.mkdir()
            inputs = root / 'inputs.jsonl'; inputs.write_text('{}\n')
            seen = []
            def client(command, **kwargs):
                seen.append(command)
                if counter is not None:
                    target = case / 'run-1/oc'; target.mkdir()
                    (target / 'counter.json').write_text(json.dumps(counter))
                return types.SimpleNamespace(returncode=0)
            with mock.patch.object(run_acceptance.subprocess, 'run', client):
                with self.assertRaises((FileNotFoundError, RuntimeError, ValueError)):
                    run_acceptance.run_case(
                        root, case, inputs, 1, 204800, 204800, 256, streaming,
                        Path('/fake/bin/evalscope'), Path('/fake/model'), 8730,
                        {}, object(), True, {}, 'performance', oc_observer=True)
            return seen

    def test_missing_counter_rejected_and_wrapper_selected(self):
        seen = self.exercise(None)
        self.assertEqual(len(seen), 1)
        self.assertEqual(Path(seen[0][1]).name, 'oc_evalscope.py')
        self.assertIn('--evidence', seen[0])
        self.assertIn('--no-test-connection', seen[0])

    def test_failed_counter_rejected(self):
        self.assertEqual(len(self.exercise({'complete': False, 'error': 'bad'})), 1)

    def test_nonstream_rejected_before_client(self):
        self.assertEqual(self.exercise(None, streaming=False), [])


if __name__ == '__main__':
    unittest.main()
