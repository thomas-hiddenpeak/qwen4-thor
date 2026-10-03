"""Host-only partition admission attacks; no real model or service is read."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import audit_offload_matrix as audit
import offload_policy as policy
import test_offload_matrix_audit as matrix_fixture
from run_budget_experiment import experiment_environment
from run_offload_lifecycle import contract_environment, reference_identity


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bind_numerical_execution(root, report, test_binary, start, end):
    identity = {field: report[field] for field in
        ('runtime_source_commit', 'runtime_binary_sha256', 'test_binary_sha256')}
    build_path, run_path = root / 'test-build.json', root / 'test-execution.json'
    build_path.write_text(json.dumps(identity))
    run_path.write_text(json.dumps(dict(identity, returncode=0,
                                        started_t=start, ended_t=end)))
    report.update(test_binary_path=str(test_binary),
                  test_build_identity_path=str(build_path),
                  test_build_identity_sha256=digest(build_path),
                  execution_record_path=str(run_path),
                  execution_record_sha256=digest(run_path),
                  started_t=start, ended_t=end)
    return report


def forward(layer=0, **changes):
    row = dict(layer=layer, T=8192, applied=1, fallback=0, reason='candidate',
               chunks=8, singleton_chunks=1, work_used=100, work_budget=2621440,
               metadata_bytes=300)
    row.update(changes)
    return ('[q4t][residency][partition] layer={layer} T={T} '
            'policy=min_new_csr_v1 requested=1 applied={applied} '
            'fallback={fallback} reason={reason} chunks={chunks} '
            'singleton_chunks={singleton_chunks} work_used={work_used} '
            'work_budget={work_budget} metadata_bytes={metadata_bytes}').format(**row)


def runtime_log(state, extra=''):
    name = 'min_new_csr_v1' if state else 'original'
    text = ('[q4t][residency] chunk_order=0 policy=original\n'
            f'[q4t][residency] partition={state} policy={name}\n')
    if state:
        text += '\n'.join(forward(i) for i in range(48)) + '\n'
    return text + extra


class PolicyContracts(unittest.TestCase):
    def test_partition_scrubs_inherited_axis_and_precision_flags(self):
        env = experiment_environment(0, {'PATH': '/bin',
            'Q4T_MOE_CHUNK_ORDER': '1', 'Q4T_MOE_PARTITION': '0',
            'Q4T_FP8_TEST': '1'}, 1, 'partition')
        self.assertEqual(env['Q4T_MOE_CHUNK_ORDER'], '0')
        self.assertEqual(env['Q4T_MOE_PARTITION'], '1')
        self.assertNotIn('Q4T_FP8_TEST', env)
        self.assertEqual(env['PATH'], '/bin')

    def test_cannot_enable_both_axes_or_implicit_partition(self):
        for order, partition, axis in ((1, 1, 'partition'),
                (1, 0, 'partition'), (0, 1, 'chunk-order'),
                (0, 0, 'unknown'), (False, 0, 'partition')):
            with self.subTest(order=order, partition=partition, axis=axis):
                with self.assertRaises(ValueError):
                    policy.policy_environment(order, partition, axis)

    def test_old_chunk_evidence_cannot_be_relabelled_as_partition(self):
        old = {'chunk_order': 1, 'effective_environment':
               policy.policy_environment(1)}
        with self.assertRaisesRegex(ValueError, 'another policy axis'):
            policy.check_policy_protocol(old, 1, 'partition')
        changed = dict(old, policy_axis='partition', partition=1, chunk_order=0)
        with self.assertRaisesRegex(ValueError, 'effective policy environment'):
            policy.check_policy_protocol(changed, 1, 'partition')

    def test_48_layer_nonfallback_execution_is_required(self):
        evidence = policy.partition_path_evidence(runtime_log(1), 1)
        self.assertTrue(evidence['runtime_eligible'])
        self.assertEqual(evidence['candidate_forwards'], 48)
        self.assertEqual(evidence['singleton_subchunks'], 48)
        self.assertFalse(evidence['numerical_acceptance'])
        partial = runtime_log(1).replace(forward(47), '')
        self.assertFalse(policy.partition_path_evidence(partial, 1)
                         ['runtime_eligible'])

    def test_budget_fallback_is_valid_no_go_not_invalid_evidence(self):
        text = runtime_log(1, forward(applied=0, fallback=1, reason='budget',
                                      work_used=2621440))
        evidence = policy.partition_path_evidence(text, 1)
        self.assertFalse(evidence['runtime_eligible'])
        self.assertEqual(evidence['fallback_forwards'], 1)

    def test_decode_bypass_is_not_budget_fallback(self):
        decode = forward(T=1, chunks=1, singleton_chunks=1, applied=0,
                         fallback=0, reason='decode', work_used=0)
        decode = decode.replace('work_budget=2621440', 'work_budget=0')
        decode = decode.replace('metadata_bytes=300', 'metadata_bytes=0')
        evidence = policy.partition_path_evidence(runtime_log(1, decode), 1)
        self.assertTrue(evidence['runtime_eligible'])
        self.assertEqual(evidence['reasons']['decode'], 1)

    def test_unsupported_prefill_is_valid_no_go(self):
        extra = forward(applied=0, reason='unsupported')
        self.assertFalse(policy.partition_path_evidence(runtime_log(1, extra), 1)
                         ['runtime_eligible'])

    def test_missing_activation_or_malformed_records_are_invalid(self):
        for text in (runtime_log(1).replace('partition=1', 'partition=0'),
                     runtime_log(1, forward(work_used=2621441)),
                     runtime_log(1, forward(reason='claimed')),
                     runtime_log(1, forward(work_budget=200)),
                     runtime_log(1, forward(work_used=0)),
                     runtime_log(1, forward(applied=0, fallback=1,
                                           reason='budget', work_used=100)),
                     runtime_log(1, forward(T=1)),
                     runtime_log(1, forward(T=8193, work_budget=2621760)),
                     runtime_log(1, forward(layer=48)),
                     runtime_log(1, '[q4t][residency][partition] truncated')):
            with self.assertRaises(ValueError):
                policy.partition_path_evidence(text, 1)

    def test_partition_off_must_not_emit_candidate_forward_records(self):
        self.assertTrue(policy.partition_path_evidence(runtime_log(0), 0)
                        ['runtime_eligible'])
        with self.assertRaisesRegex(ValueError, 'baseline unexpectedly'):
            policy.partition_path_evidence(runtime_log(0, forward()), 0)

    def test_business_identity_varies_only_partition_not_chunk_order(self):
        work = audit.ROOT / '.q4t-work'
        with tempfile.TemporaryDirectory(dir=work) as temporary:
            root = Path(temporary)
            for name in ('config.json', 'model.safetensors.index.json',
                         'requests.jsonl', 'manifest.json'):
                (root / name).write_text('{}')
            identities = []
            for state in (0, 1):
                env = contract_environment(0, 'business', state, 'partition')
                identities.append(reference_identity(root,
                    root / 'requests.jsonl', root / 'manifest.json', env,
                    'partition'))
            self.assertEqual(*identities)
            self.assertEqual(identities[0]['policy_axis'], 'partition')
            self.assertEqual(identities[0]['comparable_q4t_environment']
                             ['Q4T_MOE_CHUNK_ORDER'], '0')


class NumericalAdmissionContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=audit.ROOT / '.q4t-work')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        log, test_binary = self.root / 'test.log', self.root / 'q4t_tests'
        log.write_text('frozen synthetic tests passed\n')
        test_binary.write_text('synthetic distinct test binary')
        self.path = self.root / 'numerical.json'
        self.plan = dict(policy_axis='partition', frozen_at=0,
                         runtime_binary_sha256='a' * 64,
                         runtime_source_commit='b' * 40,
                         tool_sha256={name: 'c' * 64 for name in
                             policy.RUN_TOOLS + ('offload_policy.py',)},
                         numerical_evidence_path=str(self.path))
        self.plan['required_numerical_tests'] = ['partition map', 'singleton dispatch']
        self.report = dict(schema=1, status='PASS_NUMERICAL_CONTRACTS',
            passed=True, runtime_binary_sha256='a' * 64,
            runtime_source_commit='b' * 40, test_binary_sha256=digest(test_binary),
            required_tests=['partition map', 'singleton dispatch'],
            results=dict(passed=2, failed=0, skipped=0), recorded_t=15,
            source_sha256={str(test_binary): digest(test_binary)},
            logs=[dict(path=str(log), sha256=digest(log))])
        bind_numerical_execution(self.root, self.report, test_binary, 11, 14)

    def check(self, report=None, expected=None):
        self.path.write_text(json.dumps(self.report if report is None else report))
        return policy.check_numerical(self.plan, expected or digest(self.path),
                                      {}, 10, 20)

    def test_numerical_pass_binds_logs_binary_commit_and_order(self):
        policy.check_partition_plan(self.plan)
        self.assertTrue(self.check()['passed'])

    def test_failure_skip_or_wrong_runtime_never_admits_performance(self):
        for change in (dict(passed=False), dict(runtime_binary_sha256='d' * 64),
                       dict(runtime_source_commit='e' * 40),
                       dict(results=dict(passed=2, failed=0, skipped=1)),
                       dict(results=dict(passed=2, failed=False, skipped=0)),
                       dict(results=dict(passed=3, failed=0, skipped=0)),
                       dict(required_tests=['arbitrary', 'names']),
                       dict(required_tests=[]), dict(recorded_t=21),
                       dict(started_t=9), dict(ended_t=16),
                       dict(recorded_t=9), dict(recorded_t=float('nan'))):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    self.check(dict(self.report, **change))

    def test_changed_numerical_report_or_source_rejected(self):
        with self.assertRaisesRegex(ValueError, 'frozen source SHA'):
            self.check(expected='f' * 64)
        Path(self.report['logs'][0]['path']).write_text('changed')
        with self.assertRaisesRegex(ValueError, 'source SHA'):
            self.check()

    def test_test_binary_must_exist_and_match_artifact(self):
        binary = Path(self.report['test_binary_path'])
        binary.write_text('different executable')
        with self.assertRaisesRegex(ValueError, 'source SHA mismatch'):
            self.check()

    def test_report_cannot_rebind_another_build_or_execution(self):
        for name, key, change in (
                ('test_build_identity_path', 'test_build_identity_sha256',
                 dict(runtime_source_commit='f' * 40)),
                ('execution_record_path', 'execution_record_sha256',
                 dict(started_t=1, ended_t=2)),
                ('execution_record_path', 'execution_record_sha256',
                 dict(returncode=1))):
            path = Path(self.report[name])
            original = path.read_text()
            content = json.loads(original)
            content.update(change)
            path.write_text(json.dumps(content))
            report = dict(self.report, **{key: digest(path)})
            with self.assertRaises(ValueError):
                self.check(report)
            path.write_text(original)


class PartitionMatrixContracts(unittest.TestCase):
    """Real synthetic files; only previously tested pilot reuse is stubbed."""
    def setUp(self):
        self.fixture = matrix_fixture.MatrixFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.tool_sha['offload_policy.py'] = hashlib.sha256(b'policy helper').hexdigest()
        for directory, state in ((f.off, 0), (f.on, 1)):
            env = policy.policy_environment(0, state, 'partition')
            f.edit(directory / 'protocol.json', lambda p: p.update(
                policy_axis='partition', partition=state, chunk_order=0,
                effective_environment=env, tool_sha256=f.tool_sha))
            f.edit(directory / 'http/server-command.json',
                   lambda p: p.update(effective_q4t_environment=env))
            f.write(directory / 'tools/offload_policy.py', 'policy helper')
            f.write(directory / 'http/server.log', runtime_log(state))
        f.edit(f.plan, lambda p: p.update(policy_axis='partition',
            runtime_source_commit='a' * 40, tool_sha256=f.tool_sha,
            numerical_evidence_path=str(f.root / 'numerical.json'),
            required_numerical_tests=['map', 'dispatch']))
        self.plan_sha = digest(f.plan)
        f.reused['numerical'] = dict(passed=True,
            scope='frozen synthetic bounded numerical evidence only')

    def audit(self):
        f = self.fixture
        return audit.audit_matrix(f.plan, f.off, f.on, f.pilot,
            policy_axis='partition', expected_plan_sha256=self.plan_sha)

    def test_partition_full_gate_retains_all_six_tiers(self):
        result = self.audit()
        self.assertEqual(result['decision'], 'PASS_FULL_PERFORMANCE_SCREEN', result)
        self.assertEqual(len(result['tiers']), 6)
        self.assertEqual(result['tiers'][-1]['output_tokens'], 257)
        self.assertTrue(result['runtime_eligibility_passed'])
        self.assertFalse(result['performance_acceptance'])

    def test_relabelled_legacy_matrix_fails_closed(self):
        self.fixture.edit(self.fixture.on / 'protocol.json',
                          lambda p: p.pop('policy_axis'))
        result = self.audit()
        self.assertEqual(result['decision'], 'INVALID_EVIDENCE', result)
        self.assertIn('another policy axis', result['failure'])

    def test_mixed_chunk_order_is_invalid(self):
        self.fixture.edit(self.fixture.on / 'protocol.json',
                          lambda p: p.update(chunk_order=1))
        self.assertEqual(self.audit()['decision'], 'INVALID_EVIDENCE')

    def test_valid_fallback_is_no_go_with_valid_evidence(self):
        self.fixture.write(self.fixture.on / 'http/server.log',
            runtime_log(1, forward(applied=0, fallback=1, reason='budget',
                                  work_used=2621440)))
        result = self.audit()
        self.assertEqual(result['decision'], 'NO_GO_FULL_PERFORMANCE_SCREEN', result)
        self.assertTrue(result['evidence_contracts_passed'])
        self.assertFalse(result['runtime_eligibility_passed'])

    def test_unbound_helper_or_plan_is_invalid(self):
        self.fixture.write(self.fixture.on / 'tools/offload_policy.py', 'changed')
        result = self.audit()
        self.assertEqual(result['decision'], 'INVALID_EVIDENCE', result)
        self.assertIn('source SHA mismatch', result['failure'])

    def test_extra_or_missing_tool_dependency_is_invalid(self):
        for key, add in (('unexpected.py', True), ('offload_policy.py', False)):
            path = self.fixture.on / 'protocol.json'
            original = path.read_text()
            def change(p):
                if add:
                    p['tool_sha256'][key] = 'a' * 64
                else:
                    p['tool_sha256'].pop(key)
            self.fixture.edit(path, change)
            result = self.audit()
            self.assertEqual(result['decision'], 'INVALID_EVIDENCE', result)
            self.assertIn('incomplete tool source identities', result['failure'])
            path.write_text(original)

    def test_old_pilot_cannot_be_used_for_partition(self):
        f = self.fixture
        saved = dict(decision='PASS_SCREENING', screening_passed=True,
                     evidence_contracts_passed=True, quality_11_passed=True,
                     source_sha256={str(f.binary): f.binary_sha})
        f.save(f.pilot, saved)
        # Stop the matrix fixture's one stub to exercise real reuse admission.
        with mock.patch.object(audit, 'reuse_pilot', wraps=REAL_REUSE_PILOT):
            result = self.audit()
        self.assertEqual(result['decision'], 'INVALID_EVIDENCE', result)
        self.assertIn('another policy axis', result['failure'])


class PartitionPilotContracts(unittest.TestCase):
    """Complete synthetic quality/off/on evidence and numerical admission."""
    def setUp(self):
        self.owner = PartitionMatrixContracts()
        self.owner.setUp()
        self.addCleanup(self.owner.doCleanups)
        f = self.owner.fixture
        self.f = f
        self.quality = f.root / 'quality-on'
        quality_fixtures = f.root / 'quality-fixtures'
        quality_reference = f.root / 'quality-reference.json'
        for name in ('requests.jsonl', 'manifest.json'):
            f.write(quality_fixtures / name, 'synthetic fixed quality ' + name)
        f.save(quality_reference, [])
        quality_hashes = {str(path): digest(path) for path in
            (quality_fixtures / 'requests.jsonl',
             quality_fixtures / 'manifest.json', quality_reference)}
        for name, value in {
                'QUALITY_FIXTURES': quality_fixtures,
                'QUALITY_REFERENCE': quality_reference,
                'FROZEN_QUALITY_SHA256': quality_hashes,
                'PERF_FIXTURE_SHA256': f.fixture_sha['context-45056/requests.jsonl']
        }.items():
            patch = mock.patch.object(audit.pilot, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        q = json.loads((f.on / 'protocol.json').read_text())
        q.update(mode='quality', fixtures=str(quality_fixtures),
            fixture_sha256={Path(path).name: sha for path, sha in
                quality_hashes.items() if Path(path).parent == quality_fixtures},
            input_config_sha256={**f.configs, **quality_hashes},
            host_cache_max_bytes=None)
        f.save(self.quality / 'protocol.json', q)
        f.save(self.quality / 'wrapper-exit.json', dict(runner_rc=0, monitor_rc=0,
            failure=None, cleanup_failed=False,
            unit_after_cleanup=dict(LoadState='not-found'), started_t=0, ended_t=5))
        f.save(self.quality / 'http/exit.json', dict(server=0,
            http_output_checks_passed=True, failure=None, cleanup_failure=None,
            completed=11))
        f.save(self.quality / 'http/capacity.json', dict(matches_requested=True,
            requested=audit.CAPACITY, effective=audit.CAPACITY))
        server = json.loads((f.on / 'http/server-command.json').read_text())
        server['isolation']['host_cache_max_bytes'] = None
        f.save(self.quality / 'http/server-command.json', server)
        f.save(self.quality / 'runner-command.json',
               ['runner', '--reference', str(quality_reference)])
        for relative in ('http/binary.sha256', 'http/CMakeCache.txt',
                         'http/run_acceptance.py', 'http/server.log'):
            f.write(self.quality / relative, (f.on / relative).read_text())
        for name in f.tool_sha:
            f.write(self.quality / 'tools' / name,
                    (f.on / 'tools' / name).read_text())
        f.save(self.quality / 'http/results.json', [dict(id=name, success=1,
            actual_input=length, prompt_sha256=prompt, text=text,
            actual_output=count, exact_match=True, length_match=True,
            finish=['stop'], requested_capacity=audit.CAPACITY,
            effective_capacity=audit.CAPACITY)
            for name, length, prompt, text, count in audit.pilot.QUALITY_CASES])
        for directory in (f.off, f.on):
            f.edit(directory / 'protocol.json', lambda p: p.update(
                lengths=[45056], fixture_sha256={
                    'context-45056/requests.jsonl':
                        f.fixture_sha['context-45056/requests.jsonl']},
                performance_plan=dict(scope='partial')))
            f.edit(directory / 'http/exit.json', lambda p: p.update(
                completed=1, partial_performance_matrix=True,
                full_offload_matrix_completed=False))
            f.edit(directory / 'http/results.json',
                   lambda rows: rows.__setitem__(slice(None),
                       [row for row in rows if row['length'] == 45056]))
        # Candidate references the now bounded baseline summary.
        f.edit(f.on / 'protocol.json', lambda p: p['input_config_sha256'].update(
            {str(f.off / 'http/results.json'): digest(f.off / 'http/results.json')}))
        numerical = f.root / 'numerical.json'
        test_binary, log = f.root / 'q4t_tests', f.root / 'tests.log'
        f.write(test_binary, 'distinct test binary')
        f.write(log, 'synthetic numerical pass')
        report = dict(schema=1, status='PASS_NUMERICAL_CONTRACTS',
            passed=True, runtime_binary_sha256=f.binary_sha,
            runtime_source_commit='a' * 40, test_binary_sha256=digest(test_binary),
            required_tests=['map', 'dispatch'],
            results=dict(passed=2, failed=0, skipped=0), recorded_t=50,
            source_sha256={str(test_binary): digest(test_binary)},
            logs=[dict(path=str(log), sha256=digest(log))])
        bind_numerical_execution(f.root, report, test_binary, 10, 40)
        f.save(numerical, report)
        self.numerical_sha = digest(numerical)
        f.edit(f.plan, lambda p: p.update(frozen_at=-1,
            quality_reference_path=str(quality_reference),
            quality_reference_sha256=digest(quality_reference)))
        self.plan_sha = digest(f.plan)

    def compare(self):
        return audit.pilot.compare_evidence(self.f.off, self.f.on, self.quality,
            policy_axis='partition', plan_path=self.f.plan,
            expected_plan_sha256=self.plan_sha,
            numerical_evidence_sha256=self.numerical_sha)

    def test_new_axis_retains_pilot_gate_and_numerical_binding(self):
        report = self.compare()
        self.assertEqual(report['decision'], 'PASS_SCREENING', report)
        self.assertTrue(report['numerical']['passed'])
        self.assertTrue(report['runtime_eligibility_passed'])
        self.assertFalse(report['performance_acceptance'])

    def test_quality_cannot_reuse_old_chunk_order_activation(self):
        self.f.edit(self.quality / 'protocol.json',
                    lambda p: p.update(policy_axis='chunk-order', chunk_order=1))
        with self.assertRaisesRegex(ValueError, 'another policy axis'):
            self.compare()

    def test_quality_tool_snapshot_change_is_invalid(self):
        self.f.write(self.quality / 'tools/offload_policy.py', 'changed helper')
        with self.assertRaisesRegex(ValueError, 'source SHA mismatch'):
            self.compare()

    def test_runtime_fallback_is_valid_pilot_no_go(self):
        self.f.write(self.f.on / 'http/server.log',
            runtime_log(1, forward(applied=0, fallback=1, reason='budget',
                                  work_used=2621440)))
        report = self.compare()
        self.assertEqual(report['decision'], 'NO_GO', report)
        self.assertTrue(report['evidence_contracts_passed'])
        self.assertFalse(report['runtime_eligibility_passed'])


REAL_REUSE_PILOT = audit.reuse_pilot


if __name__ == '__main__':
    unittest.main()
