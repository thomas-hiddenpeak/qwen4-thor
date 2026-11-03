"""CPU-only driver refusal, process custody and concurrent budget checks."""
from concurrent.futures import ThreadPoolExecutor
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_cuda_quota_integration import common as c, verify


class DriverContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=c.BASE / 'driver-cpu')
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_quota_partition_refusals_and_unknowns(self):
        values = {'enabled': 1, 'charged_bytes': 5, 'reserved_bytes': 0,
            'live_bytes': 3, 'releasing_bytes': 0, 'pending_bytes': 2,
            'unknown_bytes': 0, 'peak_bytes': 8, 'limit_bytes': 16,
            'entry_capacity': 16384, 'stream_capacity': 256, 'poisoned': 0,
            'byte_rejections_total': 0, 'entry_rejections_total': 0,
            'stream_rejections_total': 0, 'contract_failures_total': 0,
            'backend_failures_total': 0}
        metrics = {'q4t_cuda_quota_' + key: value for key, value in values.items()}
        self.assertEqual(verify.quota_metrics(metrics), metrics)
        for key, value in [('charged_bytes', 6), ('unknown_bytes', 1),
                ('poisoned', 1), ('byte_rejections_total', 1), ('limit_bytes', 4)]:
            wrong = dict(metrics)
            wrong['q4t_cuda_quota_' + key] = value
            with self.assertRaises(RuntimeError):
                verify.quota_metrics(wrong)

    def test_probe_requires_exact_result_and_full_buffers(self):
        rows = [dict(event='device', sm_major=11, sm_minor=0, integrated=1),
            dict(event='verified', bytes=8 * 1024**2, full_buffer=True),
            dict(event='quota', label='explicitly_disabled', charged=0, pending=0,
                 live=0, peak=0, limit=0, unknown=0, poisoned=False, enabled=False),
            dict(event='result', mode='disabled', passed=True, verified_buffers=1,
                 verified_bytes=8 * 1024**2, checkpoints=1)]
        encode = lambda value: '\n'.join(map(json.dumps, value))
        self.assertTrue(verify.parse_probe('disabled', encode(rows))['passed'])
        for index, key, value in [(1, 'full_buffer', False), (1, 'bytes', 1),
                (2, 'pending', 1), (2, 'enabled', True), (3, 'passed', False)]:
            wrong = copy.deepcopy(rows)
            wrong[index][key] = value
            with self.assertRaises(RuntimeError):
                verify.parse_probe('disabled', encode(wrong))
        with self.assertRaises(RuntimeError):
            verify.parse_probe('disabled', encode(rows[:-1]))

    def child(self):
        lease = self.folder / 'lease'
        lease.mkdir()
        children = verify.Children(lease)
        folder = self.folder / 'child'
        process = children.launch(folder, ['/usr/bin/python3', '-B', '-c', 'pass'])
        children.wait(process, folder, 5)
        return children, process

    def acknowledge(self, children):
        when = time.monotonic()
        query = dict(status='OK', request_id='cpu', stdout='\n'.join(map(json.dumps,
            [dict(event='begin', monotonic=when),
             dict(event='result', monotonic=when + .01)])))
        (children.lease / 'nvml-queries.jsonl').write_text(json.dumps(query) + '\n')
        (children.lease / 'events.jsonl').write_text(json.dumps(dict(event='monitor',
            monotonic=when + .02, conflicts=[], unreadable_processes=[])) + '\n')

    def test_real_child_retained_until_guard_ack_and_reaped(self):
        children, process = self.child()
        try:
            self.assertEqual(verify.custody.proc(process.pid)['state'], 'Z')
            self.assertIsNone(process.returncode)
            self.acknowledge(children)
            children.reap(self.folder)
            self.assertFalse(Path(f'/proc/{process.pid}').exists())
            self.assertEqual(process.returncode, 0)
        finally:
            children.cleanup()

    def test_changed_retained_identity_is_not_accepted(self):
        children, process = self.child()
        try:
            self.acknowledge(children)
            children.started[0]['start_ticks'] += 1
            with self.assertRaisesRegex(RuntimeError, 'identity drift'):
                children.reap(self.folder)
            self.assertIsNone(process.returncode)
        finally:
            children.cleanup()

    def test_concurrent_request_budget_counts_before_attempt(self):
        run = verify.Run(dict(port=1, started=time.time()), self.folder / 'run')
        def attempt(_):
            try:
                run.budget('generations')
                return True
            except RuntimeError:
                return False
        with ThreadPoolExecutor(max_workers=8) as workers:
            outcomes = list(workers.map(attempt, range(8)))
        self.assertEqual(sum(outcomes), 7)
        self.assertEqual(run.counts['generations'], 7)
        self.assertEqual(len(list(run.out.glob('attempt-generations-*'))), 7)


if __name__ == '__main__':
    folder = c.BASE / 'driver-cpu'
    folder.mkdir()
    sources = {str(path.relative_to(ROOT)): c.sha(path)
               for path in Path(__file__).parent.glob('*.py')}
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(DriverContracts))
    (folder / 'tests.log').write_text(log.getvalue())
    c.save(folder / 'result.json', dict(passed=result.wasSuccessful(),
        count=result.testsRun, failures=len(result.failures), errors=len(result.errors),
        sources=sources, gpu_model_http_started=False))
    print(log.getvalue(), end='')
    raise SystemExit(0 if result.wasSuccessful() else 1)
