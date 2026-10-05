"""Synthetic sequence/runner contracts; no model, GPU, HTTP, or cache advice."""
import copy
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

import causality_protocol as protocol
from offload_policy import policy_environment
import run_budget_experiment as wrapper
import test_run_acceptance as runner_fixture

ROOT = Path(__file__).resolve().parents[2]
ORDERS = (('AS', 'CS', 'CL', 'AL'), ('CS', 'AL', 'AS', 'CL'),
          ('AL', 'CL', 'CS', 'AS'), ('CL', 'AS', 'AL', 'CS'))


def document(block=1, position=1):
    condition = ORDERS[block - 1][position - 1]
    predecessor = 1024 if condition[1] == 'S' else 8193
    return {
        'schema': 1, 'scope': 'offload_threshold_predecessor_v1',
        'phase_plan_sha256': 'b' * 64,
        'group': {
            'id': f'b{block:02d}-p{position:02d}-{condition.lower()}',
            'block': block, 'block_position': position,
            'condition': condition, 'arm': condition[0],
            'predecessor': condition[1], 'predecessor_tokens': predecessor,
            'requests': [{'position': i,
                          'role': 'conditioning' if i == 0 else 'probe',
                          'input_tokens': predecessor if i == 0 else 1024,
                          'output_tokens': 256} for i in range(4)]}}


def make_plan(value=None):
    return protocol.causality_plan(document() if value is None else value,
                                   'a' * 64)


class CausalitySchemaContracts(unittest.TestCase):
    def test_all_sixteen_groups_keep_four_positions_and_partial_scope(self):
        cases = set()
        for block in range(1, 5):
            for position in range(1, 5):
                plan = make_plan(document(block, position))
                self.assertEqual(plan['condition'], ORDERS[block - 1][position - 1])
                self.assertEqual([r['position'] for r in plan['requests']],
                                 [0, 1, 2, 3])
                self.assertEqual([r['role'] for r in plan['requests']],
                                 ['conditioning', 'probe', 'probe', 'probe'])
                self.assertTrue(plan['partial'])
                self.assertTrue(plan['partial_offload_matrix'])
                self.assertFalse(plan['full_five_tier_requested'])
                self.assertFalse(plan['full_offload_matrix_requested'])
                self.assertFalse(plan['performance_acceptance'])
                for request in plan['requests']:
                    self.assertEqual(request['max_tokens'], 256)
                    self.assertEqual(request['repeats'], 1)
                    self.assertNotIn(request['case'], cases)
                    cases.add(request['case'])
        self.assertEqual(len(cases), 64)

    def test_previous_request_is_explicit_and_probes_are_not_cold(self):
        plan = make_plan(document(1, 3))
        requests = plan['requests']
        self.assertEqual(plan['lengths'], [8193, 1024])
        self.assertEqual([r['preceding_input_tokens'] for r in requests],
                         [None, 8193, 1024, 1024])
        self.assertIsNone(requests[0]['preceding_case'])
        for previous, following in zip(requests, requests[1:]):
            self.assertEqual(following['preceding_case'], previous['case'])

    def test_duplicate_position_role_length_or_output_is_rejected(self):
        attacks = [('position', 0), ('position', True),
                   ('role', 'conditioning'), ('input_tokens', 8193),
                   ('input_tokens', True), ('output_tokens', 257),
                   ('output_tokens', True)]
        for key, value in attacks:
            with self.subTest(key=key, value=value):
                bad = document()
                bad['group']['requests'][1][key] = value
                with self.assertRaises(ValueError):
                    make_plan(bad)

    def test_reordered_missing_or_additional_requests_are_rejected(self):
        for order in ([1, 0, 2, 3], [0, 1, 2], [0, 1, 2, 3, 3]):
            bad = document()
            rows = bad['group']['requests']
            bad['group']['requests'] = [rows[i] for i in order]
            with self.subTest(order=order), self.assertRaises(ValueError):
                make_plan(bad)

    def test_mismatched_group_and_williams_position_are_rejected(self):
        attacks = [('id', '../unsafe'), ('condition', 'CL'), ('arm', 'C'),
                   ('predecessor', 'L'), ('predecessor_tokens', 8193),
                   ('predecessor_tokens', True), ('block', True),
                   ('block', 0), ('block', 5), ('block_position', True),
                   ('block_position', 2)]
        for key, value in attacks:
            bad = document()
            bad['group'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                make_plan(bad)

    def test_unknown_fields_cannot_smuggle_case_or_acceptance(self):
        for path in ((), ('group',), ('group', 'requests', 0)):
            bad = document()
            target = bad
            for key in path:
                target = target[key]
            target['case' if path else 'performance_acceptance'] = True
            with self.subTest(path=path), self.assertRaises(ValueError):
                make_plan(bad)

    def test_schema_scope_capacity_and_hash_types_are_strict(self):
        for key, value in (('schema', True), ('schema', 2),
                           ('scope', 'request_partition_history_v1'),
                           ('phase_plan_sha256', 'missing')):
            bad = document()
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                make_plan(bad)
        for capacity in (True, 8449, 208896, 262145):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                protocol.causality_plan(document(), 'a' * 64, capacity)
        for digest in (None, 0, 'a' * 63, 'A' * 64, 'x' * 64):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                protocol.causality_plan(document(), digest)

    def test_exact_sequence_bytes_and_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.q4t-work') as temporary:
            path = Path(temporary) / 'sequence.json'
            raw = json.dumps(document(), indent=2).encode() + b'\n'
            path.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            plan, observed = protocol.read_causality_sequence(path, digest)
            self.assertEqual(observed, raw)
            self.assertEqual(plan['sequence_sha256'], digest)
            path.write_bytes(raw + b' ')
            with self.assertRaisesRegex(ValueError, 'SHA256 differs'):
                protocol.load_causality_sequence(path, digest)
            raw = raw.replace(b'"schema": 1', b'"schema": 1, "schema": 1')
            path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, 'duplicate sequence JSON key'):
                protocol.load_causality_sequence(path, hashlib.sha256(raw).hexdigest())

    def test_environment_must_match_condition_without_instrumentation(self):
        for position, state in ((1, 0), (3, 1)):
            plan = make_plan(document(1, position))
            env = policy_environment(0, state, 'request-partition-log',
                                     request_partition=state,
                                     decode_partition_log_quiet=state)
            protocol.check_causality_environment(plan, env)
            for key, value in (('Q4T_MOE_DECODE_PARTITION_LOG_QUIET', str(1 - state)),
                               ('Q4T_MOE_PARTITION', str(1 - state)),
                               ('Q4T_MOE_REQUEST_PARTITION', str(1 - state)),
                               ('Q4T_MOE_L2_SLOTS', '17'),
                               ('Q4T_OFFLOAD_PHASE_DIAGNOSTICS', '1'),
                               ('Q4T_RESIDENCY_TIMING', '1')):
                with self.subTest(state=state, key=key), self.assertRaises(ValueError):
                    protocol.check_causality_environment(plan, {**env, key: value})

    def test_request_identity_keeps_each_repeated_prompt_position(self):
        plan = make_plan()
        identities = [protocol.causality_request_identity(plan, request)
                      for request in plan['requests']]
        self.assertEqual(len({r['case'] for r in identities}), 4)
        self.assertEqual(len({r['input_tokens'] for r in identities}), 1)
        self.assertTrue(all(r['sequence_sha256'] == 'a' * 64 for r in identities))
        self.assertTrue(all(r['phase_plan_sha256'] == 'b' * 64 for r in identities))


class CausalityRunnerContracts(unittest.TestCase):
    def setUp(self):
        # Reuse the synthetic SQLite/owned-server fixture, not its test methods.
        self.fixture = runner_fixture.RunnerContractTest(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        extra = self.fixture.fixtures / 'context-8193'
        extra.mkdir()
        (extra / 'requests.jsonl').write_text('{"prompt": "prompt-8193"}\n')
        self.path = self.fixture.root / 'sequence.json'
        self.configure(document(1, 3))

    def configure(self, value):
        self.doc = copy.deepcopy(value)
        self.raw = json.dumps(value, indent=2).encode() + b'\n'
        self.path.write_bytes(self.raw)
        self.digest = hashlib.sha256(self.raw).hexdigest()

    def invoke(self, *extra, mode='performance'):
        state = int(self.doc['group']['arm'] == 'C')
        env = policy_environment(0, state, 'request-partition-log',
                                 request_partition=state,
                                 decode_partition_log_quiet=state)
        with patch.dict(os.environ, env, clear=True):
            self.fixture.invoke('--max-len', '262144',
                                '--causality-sequence', str(self.path),
                                '--causality-sequence-sha256', self.digest,
                                *extra, mode=mode)

    def test_long_conditioning_then_three_probes_share_one_server(self):
        self.invoke()
        self.assertEqual(len(self.fixture.servers), 1)
        self.assertEqual(len(self.fixture.clients), 4)
        lengths = [int(c[c.index('--min-prompt-length') + 1])
                   for c in self.fixture.clients]
        self.assertEqual(lengths, [8193, 1024, 1024, 1024])
        self.assertTrue(all(c[c.index('--number') + 1] == '1'
                            for c in self.fixture.clients))
        self.assertEqual((self.fixture.out / 'causality-sequence.json').read_bytes(),
                         self.raw)
        rows = self.fixture.read('results.json')
        self.assertEqual([r['position'] for r in rows], [0, 1, 2, 3])
        self.assertEqual([r['role'] for r in rows],
                         ['conditioning', 'probe', 'probe', 'probe'])
        self.assertEqual([r['preceding_input_tokens'] for r in rows],
                         [None, 8193, 1024, 1024])
        for row in rows:
            self.assertEqual(row['sequence_sha256'], self.digest)
            response = self.fixture.read(row['case'] + '/responses.json')[0]
            self.assertEqual(response['position'], row['position'])
            self.assertEqual(response['case'], row['case'])
        status = self.fixture.read('exit.json')
        self.assertTrue(status['http_output_checks_passed'])
        self.assertEqual(status['diagnostic_scope'], protocol.CAUSALITY_SCOPE)
        self.assertFalse(status['full_five_tier_completed'])
        self.assertFalse(status['full_offload_matrix_completed'])
        self.assertFalse(status['performance_acceptance'])
        self.assertFalse(status['phase_diagnostics'])

    def test_short_conditioning_is_preserved_as_its_own_request(self):
        self.configure(document())
        self.invoke()
        rows = self.fixture.read('results.json')
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({r['case'] for r in rows}), 4)
        self.assertEqual([r['length'] for r in rows], [1024] * 4)
        self.assertEqual(rows[0]['role'], 'conditioning')
        self.assertIsNone(rows[0]['preceding_case'])

    def test_reused_response_id_stops_before_third_request(self):
        self.fixture.response_id_override = 'same-id'
        with self.assertRaisesRegex(RuntimeError, 'response id reused between cases'):
            self.invoke()
        self.assertEqual(len(self.fixture.clients), 2)
        self.assertFalse(self.fixture.read('exit.json')['http_output_checks_passed'])

    def test_failed_length_retains_first_response_and_stops(self):
        self.fixture.output_delta = -1
        with self.assertRaisesRegex(RuntimeError, 'output length'):
            self.invoke()
        self.assertEqual(len(self.fixture.clients), 1)
        case = make_plan(self.doc)['requests'][0]['case']
        self.assertEqual(self.fixture.read(case + '/responses.json')[0]['actual_output'],
                         255)
        self.assertFalse(self.fixture.read('exit.json')['http_output_checks_passed'])

    def test_sequence_tampering_stops_before_service(self):
        self.path.write_bytes(self.raw + b' ')
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke()
        self.assertEqual(self.fixture.servers, [])
        self.assertFalse(self.fixture.out.exists())

    def test_missing_sequence_hash_stops_before_service(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.fixture.invoke('--causality-sequence', str(self.path))
        self.assertEqual(self.fixture.servers, [])
        self.assertFalse(self.fixture.out.exists())

    def test_original_history_still_runs_twenty_one_requests(self):
        extra = self.fixture.fixtures / 'context-16385'
        extra.mkdir()
        (extra / 'requests.jsonl').write_text('{"prompt": "prompt-16385"}\n')
        self.fixture.invoke('--max-len', '262144', '--request-policy-sequence')
        self.assertEqual(len(self.fixture.servers), 1)
        self.assertEqual(len(self.fixture.clients), 21)
        rows = self.fixture.read('results.json')
        self.assertEqual([r['input_tokens'] for r in rows],
                         [16385, 8192, 8193, 1024, 45056, 4096, 8192] * 3)
        self.assertTrue(all(r['performance_scope'] == 'request_partition_history_v1'
                            for r in rows))
        self.assertTrue(all('group_id' not in r for r in rows))
        self.assertIsNone(self.fixture.read('exit.json')['diagnostic_scope'])
        self.assertFalse((self.fixture.out / 'causality-sequence.json').exists())

    def test_conflicting_cli_choices_stop_before_service(self):
        choices = [('--request-policy-sequence',), ('--perf-lengths', '1024'),
                   ('--perf-repeats', '1'), ('--phase-diagnostics',),
                   ('--extra-lengths', '261887'), ('--target-total', '262144')]
        for extra in choices:
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                self.invoke(*extra)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke(mode='quality')
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


class CausalityWrapperContracts(unittest.TestCase):
    def test_runner_command_uses_archived_sequence_and_omits_tier_flags(self):
        args = SimpleNamespace(
            mode='performance', binary=Path('/candidate/q4t'),
            model_dir=Path('/readonly/model'), fixtures=Path('/readonly/fixtures'),
            hot_list=Path('/readonly/hot.json'), port=8184, reference=None,
            host_cache_max_bytes=16 << 30, causality_sequence=Path('/source.json'),
            causality_sequence_sha256='a' * 64, request_policy_sequence=False)
        command = wrapper.runner_command(args, Path('/output'),
                                          'q4t-contract.service', make_plan())
        self.assertEqual(command[command.index('--causality-sequence') + 1],
                         '/output/causality-sequence.json')
        self.assertEqual(command[command.index('--causality-sequence-sha256') + 1],
                         'a' * 64)
        self.assertNotIn('--perf-lengths', command)
        self.assertNotIn('--perf-repeats', command)
        self.assertNotIn('--request-policy-sequence', command)
        self.assertNotIn('--phase-diagnostics', command)

    def test_wrapper_conflicts_and_missing_hash_stop_before_external_work(self):
        base = ['run_budget_experiment.py', '--mode', 'performance',
                '--output', '/unused', '--binary', '/unused/q4t',
                '--model-dir', '/unused/model', '--hot-list', '/unused/hot.json',
                '--fixtures', '/unused/fixtures', '--policy-axis',
                'request-partition-log', '--causality-sequence', '/unused/seq.json']
        extras = [[], ['--causality-sequence-sha256', 'a' * 64,
                       '--request-policy-sequence'],
                  ['--causality-sequence-sha256', 'a' * 64,
                   '--perf-lengths', '1024'],
                  ['--causality-sequence-sha256', 'a' * 64,
                   '--perf-repeats', '4']]
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
