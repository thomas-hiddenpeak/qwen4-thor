"""CPU checks of the actual managed-service acceptance driver's failure gates."""
import argparse
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_service import acceptance as a, package as pkg


class DriverTests(unittest.TestCase):
    def setUp(self):
        root = ROOT/'build/managed-service-driver-tests'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.patch = mock.patch.object(a, 'OUT', self.out)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def metrics(self):
        return dict.fromkeys(a.EMPTY, 0) | dict(q4t_seq_slots_free=1,
            q4t_seq_slots_total=1, q4t_structured_workers_started_total=3,
            q4t_structured_workers_reaped_total=3, q4t_requests_total=5,
            q4t_requests_success_total=5)

    def run_object(self):
        plan = dict(package=dict(package=str(self.out/'package'), manifest_sha256='a'*64),
                    unit='q4t-test.service', token='test')
        with mock.patch.object(pkg, 'verify', return_value=dict(settings=dict(port=18120))):
            return a.Run(plan)

    def test_idle_rejects_replay_failure_or_retained_resources(self):
        a.healthy_idle(self.metrics(), 5)
        for name, changed in (('q4t_requests_total', 6),
            ('q4t_requests_success_total', 4), ('q4t_active_chats', 1),
            ('q4t_structured_workers_reaped_total', 2), ('q4t_seq_slots_free', 0)):
            metrics = self.metrics()
            metrics[name] = changed
            with self.assertRaises(RuntimeError):
                a.healthy_idle(metrics, 5)

    def test_generation_budget_is_attempt_based(self):
        run = self.run_object()
        for _ in range(8):
            run.budget('generations')
        with self.assertRaisesRegex(RuntimeError, 'phase budget'):
            run.budget('generations')
        self.assertEqual(pkg.read(self.out/'counts.json')['generations'], 8)

    def test_cleanup_commands_remain_available_after_business_budget(self):
        run = self.run_object()
        run.counts['controls'] = a.CONTROL_LIMIT-8
        with self.assertRaises(RuntimeError):
            run.command('rejected', ['/usr/bin/true'])
        run.cleaning = True
        with mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, stdout='', stderr='')) as execute:
            run.command('cleanup', ['/usr/bin/true'])
        execute.assert_called_once()
        self.assertEqual(run.counts['controls'], a.CONTROL_LIMIT-7)

    def test_foreign_unit_is_never_stopped_or_unlinked(self):
        run = self.run_object()
        run.unit_owned = True
        with mock.patch.object(run, 'properties', return_value=dict(
            Description='different owner', FragmentPath='/not/our/unit')), \
             mock.patch.object(run, 'command') as command:
            with self.assertRaisesRegex(RuntimeError, 'ownership changed'):
                run.cleanup()
            command.assert_not_called()

    def test_partial_wire_retained_and_never_committed(self):
        run = self.run_object()
        response = mock.Mock()
        response.status = 200
        response.getheaders.return_value = [('Content-Type', 'text/event-stream')]
        response.headers.get_content_type.return_value = 'text/event-stream'
        response.read.side_effect = [b'data: {"id":"unfinished"}\n\n', b'']
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(a.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaises(ValueError):
                run.complete('partial', a.ordinary(True))
        self.assertTrue(pkg.read(self.out/'partial-http.json')['eof'])
        self.assertFalse((self.out/'partial-response.json').exists())
        self.assertEqual((self.out/'partial-wire.bin').read_bytes(),
                         b'data: {"id":"unfinished"}\n\n')
        self.assertEqual(run.counts['generations'], 1)

    def test_fresh_requests_and_fixed_output_budget(self):
        one, two = a.ordinary(), a.ordinary(True)
        self.assertNotEqual(one['request_id'], two['request_id'])
        self.assertNotEqual(one['cancel_token'], two['cancel_token'])
        self.assertEqual(one['max_tokens'], 128)
        schema = a.strict_json()['response_format']['json_schema']['schema']
        self.assertEqual(schema['properties']['ok']['enum'], [True])
        self.assertFalse(schema['additionalProperties'])

    def test_failed_worker_and_excess_starts_cannot_become_ready(self):
        run = self.run_object()
        run.run_dir.mkdir()
        for state in (dict(state='FAILED', attempts=1, error='fixed failure'),
                      dict(state='READY', attempts=4)):
            pkg.save(run.run_dir/'status.json', state)
            with self.assertRaises(RuntimeError):
                run.observe_status()

    def test_single_controller_and_lease_identity_required(self):
        run = self.run_object()
        run.run_dir.mkdir()
        pkg.save(run.run_dir/'controller.json', dict(owner=dict(pid=1)))
        run.controller = dict(owner=dict(pid=2))
        with self.assertRaisesRegex(RuntimeError, 'controller changed'):
            run.bind({}, 'must-not-bind')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = pkg.private_directory(args.output)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DriverTests)
    names = [test.id() for test in suite]
    with (output/'unittest.log').open('w') as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    sources = ('tools/serve_service/acceptance.py', 'tools/serve_service/test_acceptance.py')
    pkg.save(output/'result.json', dict(passed=result.wasSuccessful(),
        tests_run=result.testsRun, tests=names, skipped=result.skipped,
        sources={name:pkg.sha(ROOT/name) for name in sources},
        model_starts=0, http_requests=0))
    print(json.dumps(dict(passed=result.wasSuccessful(), tests=result.testsRun)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
