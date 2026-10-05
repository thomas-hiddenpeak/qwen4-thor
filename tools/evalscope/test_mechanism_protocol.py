"""Bounded observation contracts with synthetic HTTP; no model or cache use."""
from contextlib import redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import causality_protocol
import mechanism_protocol as protocol
from offload_policy import policy_environment
import run_budget_experiment as wrapper
import test_run_acceptance as runner_fixture

ROOT = Path(__file__).resolve().parents[2]
ORDER = [('AS', 'off'), ('AS', 'on'), ('CS', 'on'), ('CS', 'off'),
         ('CL', 'off'), ('CL', 'on'), ('AL', 'on'), ('AL', 'off')]


def document(index=2):
    condition, observation = ORDER[index - 1]
    length = 1024 if condition[1] == 'S' else 8193
    return {
        'schema': 1, 'scope': 'offload_cache_mechanism_v1',
        'phase_plan_sha256': 'b' * 64, 'trace_max_mib': 128,
        'group': {
            'id': f'm{index:02d}-{condition.lower()}-{observation}',
            'index': index, 'pair_index': (index + 1) // 2,
            'pair_position': 1 + (index - 1) % 2, 'condition': condition,
            'arm': condition[0], 'predecessor': condition[1],
            'predecessor_tokens': length, 'observation': observation,
            'requests': [{'position': position,
                          'role': 'conditioning' if position == 0 else 'probe',
                          'input_tokens': length if position == 0 else 1024,
                          'output_tokens': 256} for position in range(4)]}}


def make_plan(index=2):
    return protocol.mechanism_plan(document(index), 'a' * 64)


class MechanismProtocolContracts(unittest.TestCase):
    def test_all_eight_cells_derive_unique_ordered_cases(self):
        plans = [make_plan(index) for index in range(1, 9)]
        self.assertEqual([(p['condition'], p['observation']) for p in plans],
                         ORDER)
        self.assertEqual(sum(p['phase_diagnostics'] for p in plans), 4)
        cases = [r['case'] for plan in plans for r in plan['requests']]
        self.assertEqual(len(set(cases)), 32)
        for plan in plans:
            self.assertFalse(plan['performance_acceptance'])
            self.assertFalse(plan['full_five_tier_requested'])
            self.assertFalse(plan['full_offload_matrix_requested'])
            self.assertEqual([r['max_tokens'] for r in plan['requests']],
                             [256] * 4)
            self.assertIsNone(plan['requests'][0]['preceding_case'])
            self.assertEqual(plan['requests'][1]['preceding_input_tokens'],
                             plan['predecessor_tokens'])
            self.assertEqual(plan['requests'][3]['preceding_input_tokens'],
                             1024)

    def test_schema_unknown_fields_types_scope_and_quota_rejected(self):
        mutations = [('schema', True), ('schema', 2), ('trace_max_mib', True),
                     ('trace_max_mib', 129), ('trace_max_mib', 0),
                     ('scope', causality_protocol.CAUSALITY_SCOPE),
                     ('phase_plan_sha256', 'B' * 64), ('extra', 0)]
        for key, value in mutations:
            value_document = document()
            value_document[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                protocol.mechanism_plan(value_document, 'a' * 64)
        for capacity in (True, 8449, 208896, 262145):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                protocol.mechanism_plan(document(), 'a' * 64, capacity)

    def test_group_labels_cannot_relabel_order_or_observation(self):
        mutations = [('index', True), ('index', 0), ('index', 9),
                     ('pair_index', 2), ('pair_position', 1),
                     ('condition', 'CS'), ('arm', 'C'), ('predecessor', 'L'),
                     ('predecessor_tokens', 8193), ('observation', 'off'),
                     ('id', 'm01-as-off'), ('extra', 0)]
        for key, value in mutations:
            value_document = document()
            value_document['group'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                protocol.mechanism_plan(value_document, 'a' * 64)

    def test_request_order_length_output_and_extra_fields_rejected(self):
        for key, value in [('position', True), ('position', 2),
                           ('role', 'conditioning'), ('input_tokens', 8193),
                           ('output_tokens', 255), ('extra', 0)]:
            value_document = document(6)
            value_document['group']['requests'][1][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                protocol.mechanism_plan(value_document, 'a' * 64)
        for count in (3, 5):
            value_document = document()
            requests = value_document['group']['requests']
            value_document['group']['requests'] = (requests * 2)[:count]
            with self.subTest(count=count), self.assertRaises(ValueError):
                protocol.mechanism_plan(value_document, 'a' * 64)

    def test_exact_bytes_and_duplicate_json_keys_are_bound(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.q4t-work') as temporary:
            path = Path(temporary) / 'sequence.json'
            raw = json.dumps(document(), indent=2).encode() + b'\n'
            digest = hashlib.sha256(raw).hexdigest()
            path.write_bytes(raw)
            plan, observed = protocol.read_mechanism_sequence(path, digest)
            self.assertEqual(observed, raw)
            self.assertEqual(plan['sequence_sha256'], digest)
            path.write_bytes(raw + b' ')
            with self.assertRaisesRegex(ValueError, 'SHA256 differs'):
                protocol.read_mechanism_sequence(path, digest)
            raw = raw.replace(b'"schema": 1', b'"schema": 1, "schema": 1')
            path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, 'duplicate mechanism JSON key'):
                protocol.read_mechanism_sequence(
                    path, hashlib.sha256(raw).hexdigest())

    def test_environment_matches_bundle_and_strips_inherited_experiments(self):
        for index in range(1, 9):
            plan = make_plan(index)
            env = protocol.mechanism_environment(plan, {
                'PATH': '/preserved', 'Q4T_MOE_DUMP': '1',
                'Q4T_OFFLOAD_PHASE_DIAGNOSTICS': '1', 'Q4T_RESIDENCY_TIMING': '1',
                'Q4T_FP8_BAD': '1'})
            self.assertEqual(env['PATH'], '/preserved')
            self.assertNotIn('Q4T_MOE_DUMP', env)
            self.assertNotIn('Q4T_FP8_BAD', env)
            state = str(int(plan['arm'] == 'C'))
            self.assertEqual(env['Q4T_MOE_PARTITION'], state)
            self.assertEqual(env['Q4T_MOE_REQUEST_PARTITION'], state)
            self.assertEqual(env['Q4T_MOE_DECODE_PARTITION_LOG_QUIET'], state)
            for key in ('Q4T_OFFLOAD_PHASE_DIAGNOSTICS', 'Q4T_RESIDENCY_TIMING'):
                self.assertEqual(env.get(key),
                                 '1' if plan['observation'] == 'on' else None)
            protocol.check_mechanism_environment(plan, env)
            with self.assertRaises(ValueError):
                protocol.check_mechanism_environment(plan,
                    {**env, 'Q4T_MOE_MIRROR_K': '7'})
            with self.assertRaises(ValueError):
                protocol.check_mechanism_environment(plan,
                    {**env, 'Q4T_UNFROZEN': '1'})

    def test_old_policy_axis_instrumentation_rule_is_unchanged(self):
        with self.assertRaisesRegex(ValueError, 'requires partition axis'):
            policy_environment(0, 1, 'request-partition-log', True, 1, 1)

    def test_request_identity_preserves_each_repeated_prompt_position(self):
        plan = make_plan()
        rows = [protocol.mechanism_request_identity(plan, request)
                for request in plan['requests']]
        self.assertEqual([r['position'] for r in rows], [0, 1, 2, 3])
        self.assertEqual(len({r['case'] for r in rows}), 4)
        self.assertTrue(all(r['observation'] == 'on' and
                            r['sequence_sha256'] == 'a' * 64 and
                            r['phase_plan_sha256'] == 'b' * 64 for r in rows))


class MechanismRunnerContracts(unittest.TestCase):
    def setUp(self):
        self.fixture = runner_fixture.RunnerContractTest(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        extra = self.fixture.fixtures / 'context-8193'
        extra.mkdir()
        (extra / 'requests.jsonl').write_text('{"prompt": "prompt-8193"}\n')
        self.hot = self.fixture.root / 'hot.json'
        self.hot.write_text('{}')
        self.path = self.fixture.root / 'sequence.json'
        self.commands = []
        original_server = self.fixture.fake_server

        def server(command, **kwargs):
            self.commands.append(command)
            return original_server(command, **kwargs)

        self.fixture.fake_server = server
        self.freeze()

    def freeze(self, index=2):
        self.doc = document(index)
        self.raw = json.dumps(self.doc, indent=2).encode() + b'\n'
        self.path.write_bytes(self.raw)
        self.digest = hashlib.sha256(self.raw).hexdigest()

    def invoke(self, *extra, mode='performance'):
        self.fixture.invoke('--max-len', '262144', '--moe-resident-slots', '256',
            '--moe-hot-list', str(self.hot), '--mechanism-sequence', str(self.path),
            '--mechanism-sequence-sha256', self.digest, *extra, mode=mode)

    def test_observation_on_runs_four_requests_and_binds_trace_bundle(self):
        self.freeze(6)
        self.invoke()
        self.assertEqual(len(self.fixture.servers), 1)
        self.assertEqual(len(self.fixture.clients), 4)
        self.assertEqual([int(c[c.index('--min-prompt-length') + 1])
                          for c in self.fixture.clients], [8193, 1024, 1024, 1024])
        self.assertTrue(all(c[c.index('--number') + 1] == '1'
                            for c in self.fixture.clients))
        self.assertEqual((self.fixture.out / 'mechanism-sequence.json').read_bytes(),
                         self.raw)
        command = self.commands[0]
        self.assertEqual(command[command.index('--moe-trace-dir') + 1],
                         str(self.fixture.out / 'trace'))
        workload = command[command.index('--moe-trace-workload') + 1]
        self.assertEqual(Path(workload).read_bytes(), self.raw)
        self.assertEqual(command[command.index('--moe-trace-max-mib') + 1], '128')
        self.assertFalse((self.fixture.out / 'trace').exists())
        env = self.fixture.servers[0].kwargs['env']
        protocol.check_mechanism_environment(make_plan(6), env)
        rows = self.fixture.read('results.json')
        self.assertEqual([r['position'] for r in rows], [0, 1, 2, 3])
        for row in rows:
            response = self.fixture.read(row['case'] + '/responses.json')[0]
            self.assertEqual(response['group_id'], 'm06-cl-on')
            self.assertEqual(response['sequence_sha256'], self.digest)
            self.assertEqual(response['observation'], 'on')
        status = self.fixture.read('exit.json')
        self.assertTrue(status['http_output_checks_passed'])
        self.assertTrue(status['phase_diagnostics'])
        self.assertEqual(status['diagnostic_scope'], protocol.MECHANISM_SCOPE)
        self.assertFalse(status['performance_acceptance'])
        self.assertFalse(status['full_five_tier_completed'])
        self.assertFalse(status['full_offload_matrix_completed'])

    def test_observation_off_removes_inherited_bundle_and_creates_no_trace(self):
        self.freeze(1)
        with patch.dict(os.environ, {'Q4T_OFFLOAD_PHASE_DIAGNOSTICS': '1',
                                    'Q4T_RESIDENCY_TIMING': '1',
                                    'Q4T_MOE_DUMP': '1'}):
            self.invoke()
        self.assertEqual(len(self.fixture.clients), 4)
        self.assertNotIn('--moe-trace-dir', self.commands[0])
        self.assertNotIn('--moe-trace-workload', self.commands[0])
        self.assertNotIn('--moe-trace-max-mib', self.commands[0])
        self.assertFalse((self.fixture.out / 'trace').exists())
        protocol.check_mechanism_environment(
            make_plan(1), self.fixture.servers[0].kwargs['env'])
        self.assertFalse(self.fixture.read('exit.json')['phase_diagnostics'])

    def test_duplicate_response_id_stops_before_third_request(self):
        self.fixture.response_id_override = 'same-id'
        with self.assertRaisesRegex(RuntimeError, 'response id reused between cases'):
            self.invoke()
        self.assertEqual(len(self.fixture.clients), 2)
        self.assertFalse(self.fixture.read('exit.json')['http_output_checks_passed'])

    def test_failed_length_preserves_response_and_stops(self):
        self.fixture.output_delta = -1
        with self.assertRaisesRegex(RuntimeError, 'output length'):
            self.invoke()
        self.assertEqual(len(self.fixture.clients), 1)
        case = make_plan()['requests'][0]['case']
        self.assertEqual(self.fixture.read(case + '/responses.json')[0]['actual_output'],
                         255)

    def test_sequence_tampering_stops_before_output_and_service(self):
        self.path.write_bytes(self.raw + b' ')
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke()
        self.assertFalse(self.fixture.out.exists())
        self.assertEqual(self.fixture.servers, [])

    def test_conflicting_selections_and_observation_flags_rejected(self):
        choices = [('--request-policy-sequence',), ('--perf-lengths', '1024'),
                   ('--perf-repeats', '1'), ('--phase-diagnostics',),
                   ('--extra-lengths', '261887'), ('--target-total', '262144'),
                   ('--moe-trace-dir', '/unused'),
                   ('--moe-trace-workload', '/unused.json'),
                   ('--moe-trace-max-mib', '128'),
                   ('--moe-trace-max-mib', '1024'),
                   ('--causality-sequence', '/unused.json'),
                   ('--moe-resident-slots', '255')]
        for extra in choices:
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                self.invoke(*extra)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke(mode='quality')
        self.assertEqual(self.fixture.servers, [])
        self.assertFalse(self.fixture.out.exists())

    def test_missing_hash_and_build_output_rejected_before_service(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.fixture.invoke('--mechanism-sequence', str(self.path))
        self.fixture.out = self.fixture.root / 'build/result'
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke()
        self.assertEqual(self.fixture.servers, [])
        self.assertFalse(self.fixture.out.exists())

    def test_existing_evidence_is_never_overwritten(self):
        self.fixture.out.mkdir(parents=True)
        marker = self.fixture.out / 'first-failure.json'
        marker.write_text('preserve')
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke()
        self.assertEqual(marker.read_text(), 'preserve')
        self.assertEqual(self.fixture.servers, [])


class MechanismWrapperContracts(unittest.TestCase):
    def test_wrapper_arm_mismatch_stops_before_external_work(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.q4t-work') as temporary:
            path = Path(temporary) / 'sequence.json'
            raw = json.dumps(document(6)).encode()
            path.write_bytes(raw)
            command = ['run_budget_experiment.py', '--mode', 'performance',
                '--output', '/unused', '--binary', '/unused/q4t',
                '--model-dir', '/unused/model', '--hot-list', '/unused/hot.json',
                '--fixtures', '/unused/fixtures', '--policy-axis',
                'request-partition-log', '--mechanism-sequence', str(path),
                '--mechanism-sequence-sha256', hashlib.sha256(raw).hexdigest()]
            with patch.object(sys, 'argv', command), \
                    patch.object(wrapper, 'find_pids') as processes, \
                    patch.object(wrapper.subprocess, 'run') as external, \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                try:
                    wrapper.main()
                finally:
                    processes.assert_not_called()
                    external.assert_not_called()

    def test_runner_derives_bundle_from_archived_sequence_without_manual_flags(self):
        args = SimpleNamespace(
            mode='performance', binary=Path('/candidate/q4t'),
            model_dir=Path('/readonly/model'), fixtures=Path('/readonly/fixtures'),
            hot_list=Path('/readonly/hot.json'), port=8184, reference=None,
            host_cache_max_bytes=16 << 30, mechanism_sequence=Path('/source.json'),
            mechanism_sequence_sha256='a' * 64, phase_diagnostics=True)
        command = wrapper.runner_command(args, Path('/output'),
                                          'q4t-contract.service', make_plan())
        self.assertEqual(command[command.index('--mechanism-sequence') + 1],
                         '/output/mechanism-sequence.json')
        self.assertEqual(command[command.index('--mechanism-sequence-sha256') + 1],
                         'a' * 64)
        for flag in ('--perf-lengths', '--perf-repeats', '--phase-diagnostics',
                     '--moe-trace-dir', '--causality-sequence'):
            self.assertNotIn(flag, command)

    def test_wrapper_conflicts_and_missing_hash_stop_before_external_work(self):
        base = ['run_budget_experiment.py', '--mode', 'performance',
                '--output', '/unused', '--binary', '/unused/q4t',
                '--model-dir', '/unused/model', '--hot-list', '/unused/hot.json',
                '--fixtures', '/unused/fixtures', '--policy-axis',
                'request-partition-log', '--mechanism-sequence', '/unused.json']
        extras = [[], ['--mechanism-sequence-sha256', 'a' * 64,
                       '--request-policy-sequence'],
                  ['--mechanism-sequence-sha256', 'a' * 64,
                   '--perf-lengths', '1024'],
                  ['--mechanism-sequence-sha256', 'a' * 64,
                   '--phase-diagnostics'],
                  ['--mechanism-sequence-sha256', 'a' * 64,
                   '--causality-sequence', '/unused2.json',
                   '--causality-sequence-sha256', 'b' * 64]]
        for extra in extras:
            with self.subTest(extra=extra), patch.object(sys, 'argv', base + extra), \
                    patch.object(wrapper, 'find_pids') as processes, \
                    patch.object(wrapper.subprocess, 'run') as external, \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                try:
                    wrapper.main()
                finally:
                    processes.assert_not_called()
                    external.assert_not_called()


if __name__ == '__main__':
    unittest.main()
