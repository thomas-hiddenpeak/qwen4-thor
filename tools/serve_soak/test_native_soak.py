"""Targeted native handoff contracts and real CPU child cleanup; no GPU."""
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_soak import native_contract as contract
from tools.serve_soak import native_report as report
from tools.serve_soak import native_stage as stage


class ContractTests(unittest.TestCase):
    def plan(self, profile='short'):
        return dict(version=1, scenario='native_soak_'+profile,
            **contract.configuration(profile), automatic_request_replay=False,
            day_run_owner='user')

    def test_profile_preserves_duration_owner_and_exact_model_work(self):
        short, day = contract.configuration('short'), contract.configuration('day')
        self.assertEqual(short['limits']['chats'], 16)
        self.assertEqual(day['seconds'], 24*3600)
        self.assertEqual(day['limits']['chats'], day['cycles']*5+1)
        for value in (short, day):
            self.assertEqual(value['limits']['starts'], 1)
            self.assertEqual(value['limits']['probes'], 0)
            self.assertEqual(value['cycles']*value['interval'], value['seconds'])
        self.assertEqual(contract.validate_plan(self.plan()), short)

    def test_plan_cannot_silently_raise_limits_or_replay(self):
        for mutation in ('limits', 'owner', 'replay', 'duration'):
            plan = self.plan()
            if mutation == 'limits': plan['limits']['starts'] += 1
            elif mutation == 'owner': plan['day_run_owner'] = 'automatic'
            elif mutation == 'replay': plan['automatic_request_replay'] = True
            else: plan['seconds'] = 1
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                contract.validate_plan(plan)

    def test_checkpoint_requires_real_time_and_all_cycles(self):
        for profile, cycles, elapsed, final in (
            ('short', 3, 59, True), ('short', 2, 60, True),
            ('short', 180, 3600, False), ('day', 60, 3599, False),
            ('day', 59, 3600, False), ('day', 1440, 86399, True)):
            with self.subTest(profile=profile, cycles=cycles, elapsed=elapsed), \
                    self.assertRaises(ValueError):
                contract.checkpoint(profile, cycles, elapsed, final=final)
        self.assertFalse(contract.checkpoint('day', 60, 3600)[
            'day_scale_stability_proved'])
        self.assertTrue(contract.checkpoint('day', 1440, 86400, final=True)[
            'day_scale_stability_proved'])

    def test_resource_growth_checks_only_original_process_quantities(self):
        rows = [dict(rss=1000, anon=100, fds=5, threads=3) for _ in range(20)]
        accepted = contract.resource_envelope(rows)
        self.assertFalse(accepted['physical_ram_peak_proved'])
        for key in contract.GROWTH:
            changed = copy.deepcopy(rows)
            changed[-1][key] += contract.GROWTH[key]+1
            with self.subTest(key=key), self.assertRaises(ValueError):
                contract.resource_envelope(changed)
        with self.assertRaises(ValueError):
            contract.resource_envelope(rows[:16])

    def test_workload_keeps_json_sse_schema_and_actual_tool(self):
        values = contract.requests('served-model')
        self.assertEqual(set(values), {'json', 'sse', 'schema', 'tool'})
        self.assertTrue(values['sse']['stream'])
        self.assertEqual(values['schema']['response_format']['json_schema'][
            'schema']['properties']['answer']['const'], 42)
        self.assertEqual(values['tool']['tool_choice'], 'required')
        self.assertTrue(all(v['model'] == 'served-model' for v in values.values()))

    def message(self):
        return dict(role='assistant', content=None, tool_calls=[dict(id='call-a',
            type='function', function=dict(name='project_document',
                arguments=json.dumps(dict(path=contract.DOCUMENT))))])

    def test_tool_continuation_binds_actual_bytes_and_original_call(self):
        request = contract.requests('model')['tool']
        original = copy.deepcopy(request)
        message = self.message()
        data = b'actual CPU project fixture'
        follow, reply, digest = contract.answer_request(request, message, data)
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(reply['tool_call_id'], 'call-a')
        self.assertEqual(follow['messages'][-2:], [message, reply])
        self.assertEqual(request, original)
        self.assertEqual(follow['tool_choice'], 'none')
        self.assertTrue(follow['stream'])

    def test_unexpected_or_duplicate_tool_is_never_executed(self):
        for kind in ('other-name', 'other-path', 'duplicate', 'malformed'):
            message = self.message()
            function = message['tool_calls'][0]['function']
            if kind == 'other-name': function['name'] = 'another_tool'
            elif kind == 'other-path': function['arguments'] = '{"path":"/etc/passwd"}'
            elif kind == 'duplicate': message['tool_calls'] *= 2
            else: function['arguments'] = '{'
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                contract.answer_request({}, message, b'not executed')

    def test_completion_rejects_truncation_wrong_quality_and_extra_tool(self):
        valid = dict(message=dict(content='42'), finish_reason='stop')
        contract.validate_completion('json', valid)
        for changed in (dict(valid, finish_reason='length'),
                        dict(valid, message=dict(content='43')),
                        dict(valid, message=dict(content='42', tool_calls=[{}]))):
            with self.assertRaises(ValueError):
                contract.validate_completion('json', changed)
        with self.assertRaises(ValueError):
            contract.validate_completion('schema', valid)


class PrivateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=stage.ROOT/'.q4t-work')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)

    def test_metrics_use_operations_listener_and_reject_unusable_snapshot(self):
        from tools.serve_ops import fixtures
        from tools.serve_ops import contracts as ops_contracts
        values = fixtures.metrics(version=5)
        values.update({name: 0 for name in ops_contracts.METRICS_V5_ADDITIONS})
        values.update(q4t_requests_total=6, q4t_requests_success_total=6,
                      q4t_seq_slots_free=1, q4t_seq_slots_total=1)
        state = dict(status=200, body=fixtures.text_metrics(values), public=0)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                public = handler.server is front
                if public:
                    state['public'] += 1
                body = state['body'].encode()
                handler.send_response(404 if public else state['status'])
                handler.send_header('Content-Type', 'text/plain')
                handler.send_header('Content-Length', str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

            def log_message(*_args):
                pass

        front = HTTPServer(('127.0.0.1', 0), Handler)
        operations = HTTPServer(('127.0.0.1', 0), Handler)
        threads = [threading.Thread(target=s.serve_forever, daemon=True)
                   for s in (front, operations)]
        for thread in threads:
            thread.start()
        try:
            actor = stage.Stage.__new__(stage.Stage)
            actor.port = front.server_port
            actor.policy = dict(upstream_operations_port=operations.server_port)
            actor.engine = SimpleNamespace(require=contract.require)
            actor.budget = mock.Mock()
            actor.output = self.path
            result = actor.metrics('cpu', 6)
            self.assertEqual(result['q4t_requests_total'], 6)
            observed = report.read(self.path/'cpu-metrics.json')['observations']
            self.assertEqual(observed[-1]['response']['body'], state['body'])
            for status, body in ((404, state['body']), (200, 'incomplete 1\n')):
                state.update(status=status, body=body)
                with self.assertRaises(ValueError):
                    actor.metrics('invalid', 6)
                saved = report.read(self.path/'invalid-metrics.json')[
                    'observations'][-1]
                self.assertEqual(saved['response']['body'], body)
                self.assertNotIn('values', saved)
            self.assertEqual(state['public'], 0)
        finally:
            for server in (front, operations):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)

    def test_budget_charges_before_work_and_refuses_another_start(self):
        budget = stage.Budget(self.path, initialize=True,
                             limits=contract.configuration('short')['limits'])
        self.assertEqual(budget.reserve('starts', 'first'), 1)
        with self.assertRaises(ValueError): budget.reserve('starts', 'replay')
        with self.assertRaises(ValueError): budget.reserve('probes', 'unexpected')
        self.assertEqual(report.read(self.path/'counts.json')['counts']['starts'], 1)

    def test_budget_event_tail_is_bounded_without_resetting_counts(self):
        budget = stage.Budget(self.path, initialize=True,
                             limits=contract.configuration('day')['limits'])
        for number in range(80): budget.reserve('chats', str(number))
        value = report.read(self.path/'counts.json')
        self.assertEqual(value['counts']['chats'], 80)
        self.assertEqual(len(value['events']), 32)
        self.assertEqual(value['events'][0]['ordinal'], 49)
        self.assertTrue(value['event_tail_only'])

    def fixture(self):
        for name in ('execution', 'capture', 'lease'): (self.path/name).mkdir()
        for name in ('plan.json', 'run.json', 'lease/exit.json'):
            (self.path/name).write_text('{}')
        (self.path/'execution/response-wire.bin').write_bytes(b'CPU wire')

    def test_original_wire_seal_detects_mutation(self):
        self.fixture()
        sealed = report.seal(self.path)
        self.assertEqual(report.verify_seal(self.path), sealed)
        (self.path/'execution/response-wire.bin').write_bytes(b'changed wire')
        with self.assertRaises(ValueError): report.verify_seal(self.path)

    def test_evidence_boundary_rejects_links_and_parent_traversal(self):
        self.fixture()
        wire = self.path/'execution/response-wire.bin'
        wire.unlink()
        wire.symlink_to(self.path/'plan.json')
        with self.assertRaises(ValueError): report.seal(self.path)
        (self.path/'seal.json').write_text(json.dumps(dict(version=1,
            files={'../outside': '0'*64})))
        with self.assertRaises(ValueError): report.verify_seal(self.path)
        with self.assertRaisesRegex(ValueError, 'records missing'):
            report.require_records(contract.configuration('short'),
                                   dict(files={'plan.json': '0'*64}))

    def test_fresh_native_entry_does_not_preload_checkout_production(self):
        code = ('import runpy,sys; runpy.run_path(sys.argv[1]); '
                'assert not any(n.startswith(("tools.serve_recovery",'
                '"tools.serve_service")) for n in sys.modules)')
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code,
            str(stage.ROOT/'tools/serve_soak/native_stage.py')],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_guard_error_reaps_only_its_actual_cpu_child(self):
        from tools.serve_soak import evidence
        original = subprocess.Popen
        children = []

        def cpu_child(_argv, **options):
            child = original([sys.executable, '-I', '-B', '-c',
                              'import time; time.sleep(20)'], **options)
            children.append(child)
            return child

        capture = mock.Mock()
        capture.check.side_effect = RuntimeError('CPU evidence failure')
        before = signal.getsignal(signal.SIGTERM)
        with mock.patch.object(stage, 'check'), \
             mock.patch.object(evidence, 'Capture', return_value=capture), \
             mock.patch.object(stage.subprocess, 'Popen', side_effect=cpu_child), \
             self.assertRaisesRegex(ValueError, 'execution or evidence'):
            stage.run_guard(dict(output=str(self.path), profile='short',
                wall_seconds=5), self.path/'unused-plan.json')
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].returncode, -signal.SIGTERM)
        self.assertFalse(Path('/proc', str(children[0].pid)).exists())
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)
        self.assertEqual(report.read(self.path/'run.json')['status'], 'FAILED')
        capture.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
