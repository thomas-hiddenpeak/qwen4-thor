"""CPU-only runner gates; no model, CUDA, NVML, cache or scope operations."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location('startup_runner', HERE / 'run.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def capacity(stage, mtp=None):
    cfg = runner.configuration(stage)
    active = int(cfg['mtp']) if mtp is None else mtp
    fields = ' '.join(f'{phase}_{key}={cfg[key]}'
                      for phase in ('requested', 'effective')
                      for key in ('max_len', 'max_seq', 'max_prefill'))
    mode = (f'[q4t] MTP loaded (k=3 max_seq={cfg["max_seq"]})\n'
            if active else '[q4t] MTP disabled; plain decode\n')
    return (f'[q4t][capacity] {fields} budget_enabled=1 budget_feasible=true\n'
            f'[q4t][capabilities] effective mtp={active} media_allowed=0 '
            f'vision_loaded=0 max_seq={cfg["max_seq"]}\n' + mode)


class RunnerContracts(unittest.TestCase):
    def setUp(self):
        artifact = ROOT / '.q4t-work/startup-plan-20261020/runner-host'
        artifact.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=artifact)
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)

    def plan(self, stage='mtp-order-v1'):
        cfg = runner.configuration(stage)
        effective = {k: v for k, v in cfg.items() if k != 'requests'}
        effective['initial_scratch_capacity'] = 4 if cfg['mtp'] else 0
        value = dict(schema=1, derivation='configuration_tensor_metadata',
                     effective=effective, count=2, device_bytes=48,
                     physical_order=dict(strategy='stable_descending_bytes',
                                         indices=[1, 0]),
                     allocations=[dict(name='a', bytes=16),
                                  dict(name='b', bytes=32)])
        path = self.out / 'plan.json'
        runner.save(path, value)
        return path

    def log(self, path):
        plan = runner.read(path)
        rows = [dict(event='plan', plan_sha256=runner.sha(path), count=2,
                     total_bytes=48)]
        for ordinal, index in enumerate((1, 0)):
            for phase in ('begin', 'end'):
                rows.append(dict(event='alloc', phase=phase, index=index,
                                 physical_ordinal=ordinal,
                                 bytes=plan['allocations'][index]['bytes'],
                                 ptr=4096 * (index + 1), rc=0))
        rows += [dict(event='prepared', status='PASS'),
                dict(event='handoff', index=0, ptr=4096, bytes=16),
                dict(event='handoff', index=1, ptr=8192, bytes=32),
                dict(event='startup_complete', status='PASS', count=2,
                     allocated=2, consumed=2, handed_live=2, freed=0,
                     unhanded_remaining=0, release_unknown=0, backend_rc=0,
                     error='none', plan_sha256=runner.sha(path))]
        for i, row in enumerate(rows):
            row.update(seq=i, mono=float(i), tid=1)
        configured = dict(event='configured', plan_sha256=runner.sha(path),
                          count=2, device_bytes=48, effective=plan['effective'])
        return ('[startup-plan] ' + json.dumps(configured) + '\n' +
                ''.join('[reserve-trace] ' + json.dumps(row) + '\n'
                        for row in rows) + '[q4t] serving on port 8841\n')

    def test_bounded_matrix_and_no_implicit_mtp_flags(self):
        self.assertEqual(len(runner.STAGES), 3)
        self.assertEqual(sum(runner.configuration(s)['requests']
                             for s in runner.STAGES), 4)
        for stage in runner.STAGES:
            cfg = runner.configuration(stage)
            command = runner.server_command(stage, self.out)
            self.assertEqual(command[0], str(runner.BINARY))
            self.assertNotIn('--mtp', command)
            self.assertNotIn('--mtp-k', command)
            self.assertEqual('--no-mtp' in command, not cfg['mtp'])
            for key in ('max_seq', 'max_prefill', 'max_len'):
                self.assertEqual(command[command.index('--' +
                                 key.replace('_', '-')) + 1], str(cfg[key]))

    def test_boundary_uses_smallest_valid_host_cache(self):
        cfg = runner.configuration('boundary-order-v1')
        self.assertEqual((cfg['max_seq'], cfg['max_prefill'], cfg['max_len'],
                          cfg['resident_slots'], cfg['load_threads'],
                          cfg['l2_slots'], cfg['mirror_k']),
                         (2, 2048, 8192, 128, 1, 1, 0))
        env = runner.environment('boundary-order-v1', self.out,
                                 dict(PATH='/bin', Q4T_BAD='1', LD_PRELOAD='x'))
        self.assertEqual(env['Q4T_MOE_L2_SLOTS'], '1')
        self.assertEqual(env['Q4T_MOE_LOAD_THREADS'], '1')
        self.assertEqual(env['Q4T_MOE_MIRROR_K'], '0')
        self.assertNotIn('Q4T_BAD', env)
        self.assertNotIn('LD_PRELOAD', env)

    def test_repair_accepts_exact_or_one_missing_file(self):
        path = self.out / 'expert.safetensors'
        path.write_bytes(bytes(32))
        expected = {str(path): 4096}
        observation = dict(complete_file_set_observed=True,
                           files=[dict(path=str(path), resident_bytes=4096)])
        self.assertEqual(runner.repair_missing_files(observation, expected), [])
        observation['files'][0]['resident_bytes'] = 0
        self.assertEqual(runner.repair_missing_files(observation, expected),
                         [path])

    def test_repair_rejects_unknown_extra_or_missing_observation(self):
        expected = {'expert.safetensors': 4096}
        for observation in (
                dict(complete_file_set_observed=False, files=[]),
                dict(complete_file_set_observed=True, files=[]),
                dict(complete_file_set_observed=True, files=[
                    dict(path='expert.safetensors', resident_bytes=8192)])):
            with self.assertRaises(RuntimeError):
                runner.repair_missing_files(observation, expected)

    def test_repair_rejects_more_than_two_files_or_one_gib(self):
        files = []
        for i in range(3):
            path = self.out / f'expert-{i}.safetensors'
            path.write_bytes(b'x')
            files.append(dict(path=str(path), resident_bytes=0))
        with self.assertRaisesRegex(RuntimeError, 'exceeds two files'):
            runner.repair_missing_files(
                dict(complete_file_set_observed=True, files=files),
                {row['path']: 4096 for row in files})
        path = Path(files[0]['path'])
        with path.open('wb') as stream:
            stream.truncate((1 << 30) + 1)
        with self.assertRaisesRegex(RuntimeError, 'exceeds two files'):
            runner.repair_missing_files(
                dict(complete_file_set_observed=True, files=files[:1]),
                {str(path): (1 << 30) + 4096})

    def test_final_gate_repairs_once_and_requires_exact_vector(self):
        path = self.out / 'expert.safetensors'
        path.write_bytes(bytes(32))
        expected = {str(path): 4096}
        before = dict(complete_file_set_observed=True,
                      files=[dict(path=str(path), resident_bytes=0)])
        after = dict(complete_file_set_observed=True,
                     files=[dict(path=str(path), resident_bytes=4096)])
        with patch.object(runner, 'verified_cache_reference', return_value=expected), \
                patch.object(runner, 'model_files', return_value=[path]), \
                patch.object(runner, 'observe_files', side_effect=[before, after]) as observe:
            runner.final_cache_gate(self.out)
            self.assertEqual(observe.call_count, 2)

        receipt = runner.read(self.out / 'cache-launch-repair.json')
        self.assertEqual((receipt['max_passes'], receipt['files'], receipt['bytes']),
                         (1, [str(path)], 32))
        with patch.object(runner, 'verified_cache_reference', return_value=expected), \
                patch.object(runner, 'model_files', return_value=[path]), \
                patch.object(runner, 'observe_files', side_effect=[before, before]) as observe:
            with self.assertRaisesRegex(RuntimeError, 'cache vector differs'):
                runner.final_cache_gate(self.out)
            self.assertEqual(observe.call_count, 2)

    def test_early_cache_hole_reaches_final_gate_without_acceptance(self):
        path = self.out / 'expert.safetensors'
        path.write_bytes(bytes(32))
        expected = {str(path): 4096}
        runner.save(self.out / 'cache-expected.json', expected)
        runner.save(self.out / 'cache-ready.json', dict(
            complete_file_set_observed=True,
            files=[dict(path=str(path), resident_bytes=0)]))
        with patch.object(runner, 'verified_cache_reference', return_value=expected), \
                patch.object(runner, 'prepare', side_effect=RuntimeError(
                    'cache vector differs: fixture')):
            gate = runner.prepare_cache(self.out)
        self.assertFalse(gate['file_vector_exact'])
        self.assertEqual(gate['deferred_to_final_gate'], [str(path)])

    def test_other_cache_prepare_failures_are_not_deferred(self):
        with patch.object(runner, 'prepare', side_effect=RuntimeError(
                'zero payload prerequisite failed')):
            with self.assertRaisesRegex(RuntimeError, 'zero payload'):
                runner.prepare_cache(self.out)

    def test_capacity_accepts_each_frozen_configuration(self):
        for stage in runner.STAGES:
            self.assertTrue(runner.capacity_evidence(capacity(stage), stage)
                            ['matches_requested'])

    def test_default_mtp_fallback_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'mode differs'):
            runner.capacity_evidence(capacity('mtp-order-v1', mtp=0), 'mtp-order-v1')

    def test_capacity_reduction_duplicate_and_budget_bypass_rejected(self):
        good = capacity('boundary-order-v1')
        for bad in (good.replace('effective_max_seq=2', 'effective_max_seq=1'),
                    good.replace('effective_max_prefill=2048',
                                 'effective_max_prefill=8192'),
                    good.replace('budget_enabled=1', 'budget_enabled=0'),
                    good + good, ''):
            with self.assertRaises(RuntimeError):
                runner.capacity_evidence(bad, 'boundary-order-v1')

    def test_plan_gate_binds_configuration_and_handoff(self):
        for stage in runner.STAGES:
            path = self.plan(stage)
            self.assertEqual(runner.check_plan(self.log(path), stage, path)
                             ['plan_sha256'], runner.sha(path))

    def test_plan_wrong_config_or_tensor_bytes_rejected(self):
        path = self.plan()
        original = runner.read(path)
        for change in ('config', 'sum', 'scratch', 'duplicate'):
            value = copy.deepcopy(original)
            if change == 'config':
                value['effective']['max_len'] = 8192
            elif change == 'sum':
                value['device_bytes'] += 1
            elif change == 'scratch':
                value['effective']['initial_scratch_capacity'] = 3
            else:
                value['allocations'][1]['name'] = 'a'
            runner.save(path, value)
            with self.assertRaises(RuntimeError):
                runner.check_plan(self.log(path), 'mtp-order-v1', path)

    def test_physical_plan_must_be_complete_stable_descending(self):
        path = self.plan()
        original = runner.read(path)
        for indices in ([0, 1], [1, 1], [1], [1, 0, 2]):
            value = copy.deepcopy(original)
            value['physical_order']['indices'] = indices
            with self.assertRaisesRegex(RuntimeError, 'stable descending'):
                runner.physical_order_evidence(self.log(path), value)
        tied = copy.deepcopy(original)
        tied['allocations'][1]['bytes'] = 16
        with self.assertRaisesRegex(RuntimeError, 'stable descending'):
            runner.physical_order_evidence(self.log(path), tied)

    def test_physical_prefix_is_censored_and_never_complete(self):
        path = self.plan()
        plan = runner.read(path)
        rows = runner.records(self.log(path), '[reserve-trace] ')[:2]
        prefix = ''.join('[reserve-trace] ' + json.dumps(row) + '\n'
                         for row in rows)
        result = runner.physical_order_evidence(prefix, plan, complete=False)
        self.assertEqual((result['started'], result['completed']), (1, 0))
        self.assertFalse(result['complete'])
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            runner.physical_order_evidence(prefix, plan)
        with self.assertRaisesRegex(RuntimeError, 'ordinal'):
            runner.physical_order_evidence(self.log(path).replace(
                '"physical_ordinal": 0', '"physical_ordinal": 7'), plan)

    def test_ready_before_handoff_and_plan_sha_mismatch_rejected(self):
        path = self.plan()
        good = self.log(path)
        before = '[q4t] serving on port 8841\n' + good.replace(
            '[q4t] serving on port 8841\n', '')
        for bad in (before, good.replace(runner.sha(path), '0' * 64),
                    good.replace('"startup_complete"', '"not_complete"')):
            with self.assertRaises(RuntimeError):
                runner.check_plan(bad, 'mtp-order-v1', path)

    def test_request_retention_rejects_free_or_reprepare(self):
        path = self.plan()
        good = self.log(path)
        self.assertEqual(runner.retained_reservation(good, path)['handed_live'], 2)
        for event in ('free', 'prepared', 'summary'):
            with self.assertRaises(RuntimeError):
                runner.retained_reservation(good + '[reserve-trace] ' +
                                            json.dumps(dict(event=event)), path)

    def test_ledger_adapter_only_removes_checked_new_boundary(self):
        path = self.plan()
        good = self.log(path)
        with patch.object(runner, 'audit_success', return_value={'status': 'PASS'}) as audit:
            self.assertEqual(runner.reservation_evidence(good, path)['status'], 'PASS')
            lines, digest, sizes = audit.call_args.args
            records = runner.records('\n'.join(lines), '[reserve-trace] ')
            self.assertEqual([r['seq'] for r in records], list(range(8)))
            self.assertEqual(sizes, [16, 32])
            self.assertEqual(digest, runner.sha(path))
        with patch.object(runner, 'audit_success') as audit:
            with self.assertRaisesRegex(RuntimeError, 'not contiguous'):
                runner.reservation_evidence(good.replace('"seq": 3', '"seq": 8'), path)
            audit.assert_not_called()

    def test_fixtures_are_bound_to_prior_sealed_success(self):
        source = self.out / 'previous'
        fixtures = source / 'run/fixtures'
        fixtures.mkdir(parents=True)
        prompt = 'CPU fixture'
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        row = dict(id='1024-10', length=1024, expected='710003',
                   prompt_sha256=digest)
        runner.save(fixtures / 'manifest.json', [row])
        (fixtures / 'requests.jsonl').write_text(json.dumps(dict(prompt=prompt)) + '\n')
        runner.save(source / 'run/hot.json', {})
        runner.save(source / 'run/reference.json',
                    [dict(id=row['id'], text='710003', prompt_sha256=digest)])
        runner.save(source / 'sealed-files.json', {
            str(p.relative_to(source)): runner.sha(p)
            for p in source.rglob('*') if p.is_file()})
        destination = self.out / 'next'
        destination.mkdir()
        with patch.object(runner, 'CACHE_SOURCE', source):
            runner.prepare_fixtures(destination)
            self.assertEqual(runner.read(destination / 'fixtures/manifest.json'), [row])
            (source / 'run/hot.json').write_text('{"0":[1]}')
            with self.assertRaisesRegex(RuntimeError, 'frozen input differs'):
                runner.prepare_fixtures(self.out / 'must-not-copy')

    def test_build_manifest_requires_every_actual_identity(self):
        root = self.out / 'root'
        base = root / '.q4t-work/stage'
        binary = base / 'build/q4t'
        binary.parent.mkdir(parents=True)
        (root / 'build').mkdir()
        default = root / 'build/q4t'
        default.write_bytes(b'default')
        binary.write_bytes(b'candidate')
        cache = binary.with_name('CMakeCache.txt')
        cache.write_bytes(b'real cache')
        source, link = base / 'source.cpp', base / 'library.a'
        source.write_bytes(b'source')
        link.write_bytes(b'object')
        def identity(path):
            return dict(path=str(path), sha256=runner.sha(path),
                        bytes=path.stat().st_size)
        build = dict(status='COMPLETED', candidate=identity(binary),
                     candidate_sha256=runner.sha(binary),
                     default_sha256=runner.sha(default),
                     real_build_cache=identity(cache),
                     source_and_plan_inputs={str(source): identity(source)},
                     link_inputs={str(link): identity(link)})
        runner.save(base / 'build.json', build)
        with patch.multiple(runner, ROOT=root, BASE=base, BINARY=binary,
                            DEFAULT_SHA=runner.sha(default),
                            BUILD_MANIFEST=base / 'build.json'):
            self.assertEqual(runner.verify_build(), build)
            link.write_bytes(b'changed')
            with self.assertRaisesRegex(RuntimeError, 'build input differs'):
                runner.verify_build()

    def history(self):
        base = self.out / 'history'
        (base / 'build').mkdir(parents=True)
        old = base / 'build/q4t'
        old.write_bytes(b'historical candidate')
        digest = runner.sha(old)
        runner.save(base / 'build.json', dict(candidate_sha256=digest))
        rows = {}
        for name in runner.HISTORY:
            rows[name] = dict(name=name, candidate_sha256=digest,
                status='RUNTIME_COMPLETED' if name == 'plain-v1'
                else 'RUNTIME_NOT_ACCEPTED',
                guard='COMPLETED' if name == 'plain-v1' else
                'ENV_ABORT' if name == 'mtp-v3' else 'COMMAND_FAILED',
                model_starts=int(name in ('plain-v1', 'mtp-v3')),
                http_requests=int(name == 'plain-v1'), manifest_sha256='sealed')
        return base, rows

    def test_budget_counts_failed_model_but_not_preparation_failures(self):
        base, rows = self.history()
        with patch.object(runner, 'BASE', base), \
                patch.object(runner, 'sealed_attempt', side_effect=rows.__getitem__):
            receipt = runner.previous_stage_gate('mtp-order-v1')
            self.assertEqual(receipt['prior_model_starts'], 2)
            self.assertEqual(receipt['prior_http_requests'], 1)
            with patch.object(runner, 'MODEL_START_LIMIT', 2):
                with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
                    runner.previous_stage_gate('mtp-order-v1')
            rows['mtp-v3']['model_starts'] = 0
            with self.assertRaisesRegex(RuntimeError, 'history differs'):
                runner.previous_stage_gate('mtp-order-v1')

    def test_new_stage_failure_and_existing_attempt_stop_the_sequence(self):
        base, rows = self.history()
        binary = base / 'ordered'
        binary.write_bytes(b'ordered')
        rows['mtp-order-v1'] = dict(name='mtp-order-v1',
            status='RUNTIME_NOT_ACCEPTED', guard='ENV_ABORT', model_starts=1,
            http_requests=0, candidate_sha256=runner.sha(binary))
        out = base / 'mtp-order-v1/run/normal/http'
        out.mkdir(parents=True)
        runner.save(out / 'exit.json', dict(http_output_checks_passed=False))
        runner.save(out / 'reservation-audit.json', dict(status='FAIL'))
        with patch.multiple(runner, BASE=base, BINARY=binary), \
                patch.object(runner, 'sealed_attempt', side_effect=rows.__getitem__):
            with self.assertRaisesRegex(RuntimeError, 'previous stage'):
                runner.previous_stage_gate('restart-order-v1')
            with self.assertRaisesRegex(RuntimeError, 'no implicit retry'):
                runner.previous_stage_gate('mtp-order-v1')

    def http_contract(self, fallback=False):
        base = self.out / 'http-stage'
        run = base / 'mtp-order-v1/run'
        fixtures = run / 'fixtures'
        fixtures.mkdir(parents=True)
        fixture = dict(id='1024-10', expected='710003', prompt_sha256='abc')
        runner.save(fixtures / 'manifest.json', [fixture])
        (fixtures / 'requests.jsonl').write_text('{"prompt":"fixture"}\n')
        binary = base / 'build/q4t'
        binary.parent.mkdir()
        binary.write_bytes(b'candidate')
        binary.with_name('CMakeCache.txt').write_text('cache')
        plan_path = self.plan()
        log = capacity('mtp-order-v1', mtp=0 if fallback else 1) + self.log(plan_path)
        observed = dict(closed=False, commands=[])

        class FakeScope:
            pid = 123
            returncode = None

            def __init__(self, command, **kwargs):
                observed['commands'].append(command)
                kwargs['log_path'].write_text(log)
                (run / 'startup-plan.json').write_bytes(plan_path.read_bytes())

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = 0

            def wait(self, timeout=None):
                return self.returncode

            def close(self):
                observed['closed'] = True

        responses = []
        def client(*args, **kwargs):
            row = dict(text='710003', actual_input=1024, actual_output=7,
                       finish=['stop'], prompt_sha256='abc',
                       response_id='chatcmpl-' + str(len(responses)))
            responses.append(row)
            return [row]
        with patch.multiple(runner, BASE=base, BINARY=binary), \
                patch.object(runner, 'verify_build', return_value={}), \
                patch.object(runner, 'IsolatedScope', FakeScope), \
                patch.object(runner, 'final_cache_gate'), \
                patch.object(runner, 'observe_files', return_value={}), \
                patch.object(runner, 'model_files', return_value=[]), \
                patch.object(runner, 'verified_cache_reference', return_value={}), \
                patch.object(runner, 'check_vector'), \
                patch.object(runner, 'run_case', side_effect=client), \
                patch.object(runner, 'reservation_evidence',
                             return_value={'status': 'PASS'}):
            rc = runner.http_worker('mtp-order-v1')
        return rc, observed, responses, runner.read(run / 'normal/http/exit.json')

    def test_mtp_http_worker_reuses_one_instance_for_two_requests(self):
        rc, observed, responses, result = self.http_contract()
        self.assertEqual(rc, 0)
        self.assertEqual(len(observed['commands']), 1)
        self.assertEqual(len(responses), 2)
        self.assertTrue(observed['closed'])
        self.assertTrue(result['http_output_checks_passed'])
        self.assertNotIn('--mtp', observed['commands'][0])
        self.assertNotIn('--no-mtp', observed['commands'][0])

    def test_http_worker_stops_fallback_without_any_http(self):
        rc, observed, responses, result = self.http_contract(fallback=True)
        self.assertEqual(rc, 1)
        self.assertEqual(responses, [])
        self.assertTrue(observed['closed'])
        self.assertFalse(result['http_output_checks_passed'])


if __name__ == '__main__':
    unittest.main()
