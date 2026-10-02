"""Runner contracts with synthetic SQLite and mocked processes; no HTTP/GPU."""
import base64
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import pickle
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import run_acceptance as runner

ROOT = Path(__file__).resolve().parents[2]


class RunnerContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='runner-host-', dir=ROOT / '.q4t-work')
        self.root = Path(self.temp.name)
        self.binary = self.root / 'build/q4t'
        self.binary.parent.mkdir()
        self.binary.write_text('synthetic binary; never executed')
        (self.binary.parent / 'CMakeCache.txt').write_text('synthetic cache')
        self.fixtures = self.root / 'fixtures'
        for length in runner.LENGTHS + [261887]:
            directory = self.fixtures / f'context-{length}'
            directory.mkdir(parents=True)
            (directory / 'requests.jsonl').write_text(json.dumps({'prompt': f'prompt-{length}'}) + '\n')
        self.out = self.root / '.q4t-work/result'
        self.clients = []
        self.servers = []
        self.client_rc = 0
        self.http_success = 1
        self.effective_length = None
        self.output_delta = 0
        self.no_database = False
        self.latency = 1.0
        self.quality = False
        self.cleanup_error = False

    def tearDown(self):
        self.temp.cleanup()

    def fake_server(self, command, **kwargs):
        length = int(command[command.index('--max-len') + 1])
        effective = self.effective_length if self.effective_length is not None else length
        text = (f'[q4t][budget]   => max_len={effective} max_seq=1\n'
                f'[q4t][capacity] requested_max_len={length} requested_max_seq=1 '
                f'requested_max_prefill=8192 effective_max_len={effective} '
                'effective_max_seq=1 effective_max_prefill=8192 '
                'budget_enabled=1 budget_feasible=true\n[q4t] serving on port 8000\n')
        if 'stdout' in kwargs:
            kwargs['stdout'].write(text)
            kwargs['stdout'].flush()
        else:
            kwargs['log_path'].write_text(text)
        snapshots = []
        instance = SimpleNamespace(pid=424242, returncode=None, terminated=False,
                                   closed=False, snapshots=snapshots, kwargs=kwargs)
        instance.poll = lambda: instance.returncode
        def terminate():
            instance.terminated = True
        def wait(timeout=None):
            instance.returncode = 0
            return 0
        def close():
            instance.closed = True
            if self.cleanup_error:
                raise RuntimeError('synthetic cleanup failure')
        instance.terminate = terminate
        instance.kill = terminate
        instance.wait = wait
        instance.close = close
        instance.snapshot = snapshots.append
        self.servers.append(instance)
        return instance

    def fake_client(self, command, **kwargs):
        self.clients.append(command)
        self.assertIn('--no-test-connection', command)
        self.assertEqual(command[command.index('--warmup-num') + 1], '0')
        if self.no_database:
            return SimpleNamespace(returncode=self.client_rc)
        def arg(name):
            return command[command.index(name) + 1]
        start = time.perf_counter()
        inputs = Path(arg('--dataset-path')).read_text().splitlines()
        number, length, tokens = int(arg('--number')), int(arg('--min-prompt-length')), int(arg('--max-tokens'))
        db_path = Path(arg('--outputs-dir')) / 'benchmark_data.db'
        with sqlite3.connect(db_path) as db:
            db.execute('create table result(success integer,prompt_tokens integer,completion_tokens integer,'
                       'response_messages text,first_chunk_latency real,latency real,request text,'
                       'start_time real,completed_time real)')
            for i in range(number):
                prompt = json.loads(inputs[i])['prompt']
                if self.quality:
                    length = int(prompt.split('-')[-1])
                messages = [{'choices': [{'delta': {'content': 'ok'},
                                           'finish_reason': 'stop' if self.quality else 'length'}]}]
                request = {'prompt': prompt, 'stream': '--stream' in command, 'max_tokens': tokens}
                encoded = base64.b64encode(pickle.dumps(messages)).decode()
                db.execute('insert into result values (?,?,?,?,?,?,?,?,?)',
                           (self.http_success, length, (2 if self.quality else tokens) + self.output_delta,
                            encoded, .1, self.latency, json.dumps(request), start, time.perf_counter()))
        return SimpleNamespace(returncode=self.client_rc)

    def invoke(self, *extra, mode='performance'):
        argv = ['run_acceptance.py', '--mode', mode, '--binary', str(self.binary),
                '--model-dir', str(self.root / 'model'), '--fixtures', str(self.fixtures),
                '--output', str(self.out), *extra]
        with ExitStack() as stack:
            stack.enter_context(patch.object(runner, 'ROOT', self.root))
            stack.enter_context(patch.object(sys, 'argv', argv))
            stack.enter_context(patch.object(runner.subprocess, 'check_output', return_value=b'synthetic metadata\n'))
            stack.enter_context(patch.object(runner.subprocess, 'Popen', side_effect=self.fake_server))
            stack.enter_context(patch.object(runner.subprocess, 'run', side_effect=self.fake_client))
            stack.enter_context(patch.dict(sys.modules, {'isolated_service': SimpleNamespace(IsolatedService=self.fake_server)}))
            stack.enter_context(redirect_stdout(io.StringIO()))
            runner.main()

    def read(self, relative):
        return json.loads((self.out / relative).read_text())

    def test_default_five_tiers_keep_batched_three(self):
        self.invoke()
        self.assertEqual(len(self.clients), 5)
        self.assertEqual([int(c[c.index('--number') + 1]) for c in self.clients], [3] * 5)
        plan = self.read('performance-plan.json')
        self.assertEqual(plan['lengths'], runner.LENGTHS)
        self.assertFalse(plan['partial'])
        status = self.read('exit.json')
        self.assertTrue(status['full_five_tier_completed'])
        self.assertFalse(status['full_offload_matrix_completed'])
        self.assertFalse(status['performance_acceptance'])

    def test_selected_tier_is_partial_with_per_request_evidence(self):
        self.invoke('--perf-lengths', '45056', '--perf-repeats', '3')
        self.assertEqual(len(self.clients), 3)
        self.assertTrue(all(c[c.index('--number') + 1] == '1' for c in self.clients))
        self.assertTrue(self.read('exit.json')['partial_performance_matrix'])
        self.assertEqual(self.read('performance-plan.json')['scope'], 'partial')
        self.assertEqual((self.out / 'server.pid').read_text().strip(), '424242')
        boundaries = self.read('context-45056/request-boundaries.json')
        self.assertEqual([r['request_index'] for r in boundaries], [1, 2, 3])
        self.assertEqual(len(self.servers[0].snapshots), 6)
        for row in boundaries:
            self.assertTrue(row['http_timing']['within_client_boundaries'])
            self.assertIn('+00:00', row['client_before']['utc'])
            self.assertLessEqual(row['client_before']['monotonic_seconds'], row['client_after']['monotonic_seconds'])
            self.assertEqual(row['actual_input'], 45056)
            self.assertEqual(row['actual_output'], 256)
        events = [json.loads(line) for line in (self.out / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(events[0]['event'], 'runner_start')
        self.assertEqual(events[-1]['event'], 'server_after_shutdown')

    def test_fewer_than_three_repeats_remains_partial(self):
        self.invoke('--perf-repeats', '1')
        self.assertTrue(self.read('exit.json')['partial_performance_matrix'])
        self.assertFalse(self.read('exit.json')['full_five_tier_completed'])

    def test_partial_repeat_count_can_use_fixed_deterministic_reference(self):
        reference = self.root / 'reference.json'
        reference.write_text(json.dumps([{'length': 45056,
            'prompt_sha256': hashlib.sha256(b'prompt-45056').hexdigest(),
            'outputs': [hashlib.sha256(b'ok').hexdigest()] * 3}]))
        self.invoke('--perf-lengths', '45056', '--perf-repeats', '1',
                    '--reference', str(reference))
        self.assertTrue(self.read('exit.json')['partial_performance_matrix'])
        self.assertTrue(self.read('exit.json')['http_output_checks_passed'])

    def test_six_tier_target_remains_257(self):
        self.invoke('--extra-lengths', '261887', '--target-total', '262144', '--max-len', '262144')
        self.assertTrue(self.read('exit.json')['full_offload_matrix_completed'])
        self.assertEqual(self.read('context-261887/responses.json')[0]['actual_output'], 257)

    def test_bad_selection_and_requested_capacity_fail_before_server(self):
        for extra in [('--perf-lengths', '45056,45056'), ('--perf-lengths', ''),
                      ('--perf-lengths', '-1'), ('--perf-repeats', '0'),
                      ('--perf-lengths', '45056', '--max-len', '45100'),
                      ('--extra-lengths', '1024'), ('--host-cache-max-bytes', '100')]:
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                self.invoke(*extra)
        self.assertFalse(self.servers)

    def test_reduced_effective_capacity_rejects_before_client(self):
        self.effective_length = 65536
        with self.assertRaisesRegex(RuntimeError, 'effective server capacity'):
            self.invoke('--perf-lengths', '45056')
        self.assertFalse(self.clients)
        self.assertFalse(self.read('capacity.json')['matches_requested'])
        self.assertFalse(self.read('exit.json')['http_output_checks_passed'])
        self.assertTrue(self.servers[0].closed)

    def test_failed_client_preserves_response_and_stops_bounded_series(self):
        self.client_rc = 7
        with self.assertRaisesRegex(RuntimeError, 'client/database/request count'):
            self.invoke('--perf-lengths', '45056')
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(len(self.read('context-45056/responses.json')), 1)
        self.assertEqual(self.read('context-45056/run-1/client-exit.json')['returncode'], 7)
        self.assertFalse(self.read('exit.json')['http_output_checks_passed'])

    def test_missing_database_preserves_empty_evidence_and_fails(self):
        self.no_database = True
        with self.assertRaisesRegex(RuntimeError, 'databases=0'):
            self.invoke('--perf-lengths', '45056')
        self.assertEqual(self.read('context-45056/responses.json'), [])

    def test_failed_http_preserves_raw_status(self):
        self.http_success = 0
        with self.assertRaisesRegex(RuntimeError, 'HTTP/timing'):
            self.invoke('--perf-lengths', '45056')
        self.assertEqual(self.read('context-45056/responses.json')[0]['success'], 0)
        self.assertEqual(len(self.clients), 1)

    def test_wrong_output_count_fails(self):
        self.output_delta = -1
        with self.assertRaisesRegex(RuntimeError, 'output length/finish'):
            self.invoke('--perf-lengths', '45056')
        self.assertEqual(len(self.clients), 1)
        self.assertFalse(self.read('exit.json')['http_output_checks_passed'])

    def test_nonfinite_metrics_fail(self):
        self.latency = float('nan')
        with self.assertRaisesRegex(RuntimeError, 'invalid HTTP performance metrics'):
            self.invoke('--perf-lengths', '45056')

    def test_systemd_interface_and_close_preserve_exact_pid(self):
        self.invoke('--perf-lengths', '45056', '--systemd-unit', 'q4t-test.service',
                    '--host-cache-max-bytes', '17179869184')
        service = self.servers[0]
        self.assertEqual(service.kwargs['memory_max'], 17179869184)
        self.assertTrue(service.closed)
        self.assertTrue(service.terminated)
        self.assertEqual((self.out / 'server.pid').read_text().strip(), '424242')
        isolation = self.read('server-command.json')['isolation']
        self.assertEqual(isolation['swap_max_bytes'], 0)
        self.assertFalse(isolation['is_total_physical_ram_limit'])

    def test_cleanup_failure_preserves_exit_and_rejects_successful_requests(self):
        self.cleanup_error = True
        with self.assertRaisesRegex(RuntimeError, 'synthetic cleanup failure'):
            self.invoke('--perf-lengths', '45056')
        status = self.read('exit.json')
        self.assertFalse(status['http_output_checks_passed'])
        self.assertIn('synthetic cleanup failure', status['cleanup_failure'])

    def test_quality_keeps_single_batched_client(self):
        self.quality = True
        prompts = ['prompt-1024', 'prompt-8192']
        manifest = [{'id': str(i), 'prompt_sha256': hashlib.sha256(p.encode()).hexdigest(),
                     'length': int(p.split('-')[-1]), 'expected': 'ok'} for i, p in enumerate(prompts)]
        (self.fixtures / 'manifest.json').write_text(json.dumps(manifest))
        (self.fixtures / 'requests.jsonl').write_text(''.join(json.dumps({'prompt': p}) + '\n' for p in prompts))
        self.invoke(mode='quality')
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.clients[0][self.clients[0].index('--number') + 1], '2')
        self.assertIsNone(self.read('exit.json')['partial_performance_matrix'])
        self.assertTrue(self.read('exit.json')['http_output_checks_passed'])


class EvidenceHelpersTest(unittest.TestCase):
    def test_capacity_unknown_duplicate_or_reduced_is_not_accepted(self):
        for text in ['', '[q4t][budget]   => max_len=1024 max_seq=1',
                     '[q4t][budget]   => max_len=4096 max_seq=1\n' * 2]:
            self.assertFalse(runner.capacity_evidence(text, 4096)['matches_requested'])
        value = runner.capacity_evidence('[q4t][budget]   => max_len=4096 max_seq=1', 4096)
        self.assertTrue(value['matches_requested'])
        self.assertIsNone(value['effective_max_prefill'])

    def test_http_timestamps_keep_clock_domain_and_unknown_boundary(self):
        a = {'monotonic_seconds': 100., 'unix_seconds': 1000000.}
        b = {'monotonic_seconds': 110., 'unix_seconds': 1000010.}
        value = runner.request_timing(101., 109., a, b)
        self.assertTrue(value['within_client_boundaries'])
        self.assertEqual(value['clock_offset_change_seconds'], 0)
        for start, end in [(0., 109.), (101., None), (109., 101.), (101., float('nan'))]:
            value = runner.request_timing(start, end, a, b)
            self.assertFalse(value['within_client_boundaries'])
            self.assertIsNone(value['start_utc_estimate'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
