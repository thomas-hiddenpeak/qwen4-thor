"""Pure host synthetic contracts; no service, GPU, real matrix, or model read.

The end-to-end fixture below stubs only reuse of the already audited pilot.
Separate tests exercise pilot decision/digest reuse. All six-tier evidence,
raw-response comparisons, timing, copied tools and references remain real files.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import audit_offload_matrix as audit


def sha(data):
    return hashlib.sha256(data).hexdigest()


class MatrixFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix='matrix-audit-contract-', dir=audit.ROOT / '.q4t-work')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model, self.hot = self.root / 'model', self.root / 'hot.json'
        self.fixtures, self.reference = self.root / 'fixtures', self.root / 'old-c0.json'
        self.off, self.on = self.root / 'matrix-off', self.root / 'matrix-on'
        self.binary = self.root / 'q4t'
        self.write(self.binary, 'synthetic binary identity only')
        self.write(self.binary.parent / 'CMakeCache.txt', 'synthetic actual build cache\n')
        self.binary_sha = sha(self.binary.read_bytes())
        configs = {self.hot: '{}', self.model / 'config.json': '{}',
                   self.model / 'model.safetensors.index.json': '{}'}
        for path, content in configs.items():
            self.write(path, content)
        self.configs = {str(p): sha(p.read_bytes()) for p in configs}
        for name, value in {'MODEL': self.model, 'HOT': self.hot,
                            'PERF_FIXTURES': self.fixtures, 'PERF_REFERENCE': self.reference,
                            'FROZEN_CONFIG_SHA256': self.configs}.items():
            patch = mock.patch.object(audit.pilot, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.tool_sha = {name: sha(('frozen ' + name).encode()) for name in audit.TOOLS}
        self.fixture_sha = {}
        old = []
        for n in audit.LENGTHS:
            relative = f'context-{n}/requests.jsonl'
            self.write(self.fixtures / relative, json.dumps({'prompt': f'prompt {n}'}) + '\n')
            self.fixture_sha[relative] = sha((self.fixtures / relative).read_bytes())
            old.append({'length': n, 'outputs': [sha(f'answer {n}'.encode())] * 3,
                        'prompt_sha256': sha(f'prompt {n}'.encode())})
        self.save(self.reference, old)
        patch = mock.patch.object(audit.pilot, 'PERF_REFERENCE_SHA256', sha(self.reference.read_bytes()))
        patch.start()
        self.addCleanup(patch.stop)
        self.plan = self.root / 'plan.json'
        self.save(self.plan, {'lengths': audit.LENGTHS, 'repeats': 3,
                             'output_tokens': {'default': 256, '261887': 257},
                             'runtime_binary_sha256': self.binary_sha,
                             'runner_timeout_s': 21600, 'request_deadline_ms': 1800000,
                             'frozen_at': 5})
        patch = mock.patch.object(audit, 'PLAN_SHA256', sha(self.plan.read_bytes()))
        patch.start()
        self.addCleanup(patch.stop)
        self.pilot = self.root / 'pilot-decision.json'
        self.save(self.pilot, {'synthetic': True})
        self.reused = {'binary_sha256': self.binary_sha, 'tool_sha256': self.tool_sha,
                       'ended_t': 10, 'decision_source': str(self.pilot)}
        patch = mock.patch.object(audit, 'reuse_pilot', return_value=self.reused)
        self.reuse_mock = patch.start()
        self.addCleanup(patch.stop)
        self.build_run(self.off, 0, 100, 111, self.reference)
        self.build_run(self.on, 1, 2100, 222, self.off / 'http/results.json')

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def save(self, path, value):
        self.write(path, json.dumps(value, indent=2) + '\n')

    def edit(self, path, change):
        value = json.loads(path.read_text())
        change(value)
        self.save(path, value)

    def build_run(self, directory, order, start, pid, reference):
        unit = f'q4t-synthetic-{order}.service'
        env = {**audit.ENVIRONMENT, 'Q4T_MOE_CHUNK_ORDER': str(order)}
        capacity = {'requested': audit.CAPACITY, 'effective': audit.CAPACITY,
                    'reported_requested': audit.CAPACITY, 'matches_requested': True,
                    'budget_enabled': '1', 'budget_feasible': 'true'}
        requests = [{'input_tokens': n, 'max_tokens': audit.target(n),
                     'total_tokens': n + audit.target(n), 'repeats': 3} for n in audit.LENGTHS]
        execution = {'requests': requests, 'lengths': audit.LENGTHS, 'repeats': 3,
                     'scope': 'six_tier', 'partial': False, 'full_offload_matrix_requested': True,
                     'client_protocol': 'one_evalscope_process_per_request'}
        protocol = {'mode': 'performance', 'chunk_order': order, 'lengths': audit.LENGTHS,
                    'repeats': 3, 'binary': str(self.binary), 'binary_sha256': self.binary_sha,
                    'host_cache_max_bytes': 16 << 30, 'swap_max_bytes': 0,
                    'cold_payload_max_resident_bytes': 0, 'runner_timeout_s': 21600,
                    'request_deadline_ms': 1800000, **audit.CAPACITY,
                    'effective_environment': env, 'unit': unit,
                    'monitor': {'interval_seconds': 1.0, 'gpu_interval_seconds': 10.0,
                                'file_cache_mode': 'endpoints',
                                'live_file_cache_observation': 'NOT_SAMPLED'},
                    'output_tokens_by_length': {str(n): audit.target(n) for n in audit.LENGTHS},
                    'output_tokens': None, 'tool_sha256': self.tool_sha,
                    'input_config_sha256': {**self.configs, str(reference): sha(reference.read_bytes()),
                        **{str(self.fixtures / k): v for k, v in self.fixture_sha.items()}},
                    'fixtures': str(self.fixtures), 'fixture_sha256': self.fixture_sha,
                    'performance_plan': {**execution, 'partial_offload_matrix': False},
                    'model_files': [{'path': str(self.model / 'config.json'), 'size': 2}],
                    'startup_timeout_s': 600, 'client_lifecycle': 'one client per request',
                    'cache_protocol': 'cold group then inherited'}
        self.save(directory / 'protocol.json', protocol)
        self.save(directory / 'http/performance-plan.json', execution)
        self.save(directory / 'http/capacity.json', capacity)
        self.save(directory / 'wrapper-exit.json', {
            'runner_rc': 0, 'monitor_rc': 0, 'failure': None, 'cleanup_failed': False,
            'unit_after_cleanup': {'LoadState': 'not-found'},
            'full_offload_matrix_completed': True, 'partial_performance_matrix': False,
            'performance_scope': 'six_tier', 'partial_offload_matrix': False,
            'started_t': start, 'ended_t': start + 1900})
        self.save(directory / 'http/exit.json', {
            'server': 0, 'http_output_checks_passed': True, 'failure': None,
            'cleanup_failure': None, 'completed': 6, 'full_offload_matrix_completed': True,
            'partial_performance_matrix': False, 'performance_scope': 'six_tier',
            'full_five_tier_completed': True})
        self.save(directory / 'runner-process-group.json', {
            'cleanup_complete': True, 'runner_reaped': True, 'failure': None,
            'returncode': 0, 'unexpected_live_descendants_after_runner_exit': False,
            'signals': [], 'after_cleanup': {'absent': True, 'live_pids': [],
                                           'zombie_pids': [], 'errors': []},
            'runner_pid': pid + 1, 'pgid': pid + 1, 'timeout_s': 21600,
            'started_t': start + 1, 'ended_t': start + 1899})
        self.save(directory / 'http/isolation/identity.json', {
            'pid': pid, 'properties': {'MainPID': str(pid), 'ControlGroup': '/system.slice/' + unit,
                                     'MemoryMax': str(16 << 30), 'MemorySwapMax': '0',
                                     'MemoryAccounting': 'yes'},
            'membership': ['0::/system.slice/' + unit]})
        self.save(directory / 'http/isolation/cleanup.json', {
            'stop_rc': 0, 'reset_rc': 1, 'unit_removed': True,
            'properties_after': {'LoadState': 'not-found', 'MainPID': '0'}})
        server = [str(self.binary), 'serve', '--no-mtp', '--port', '8172']
        for flag, value in {'--model-dir': str(self.model), '--moe-hot-list': str(self.hot),
                            '--moe-resident-slots': '256', '--max-len': '262144',
                            '--max-seq': '1', '--max-prefill': '8192',
                            '--request-deadline-ms': '1800000'}.items():
            server += [flag, value]
        self.save(directory / 'http/server-command.json', {
            'argv': server, 'effective_q4t_environment': env,
            'hot_list': {'path': str(self.hot), 'sha256': self.configs[str(self.hot)]},
            'isolation': {'systemd_unit': unit, 'host_cache_max_bytes': 16 << 30, 'swap_max_bytes': 0}})
        runner = ['python', 'runner.py']
        for flag, value in {'--reference': str(reference), '--perf-lengths': ','.join(map(str, audit.LENGTHS)),
                            '--perf-repeats': '3', '--extra-lengths': '261887',
                            '--target-total': '262144', '--systemd-unit': unit,
                            '--binary': str(self.binary), '--fixtures': str(self.fixtures),
                            '--output': str(directory / 'http')}.items():
            runner += [flag, value]
        self.save(directory / 'runner-command.json', runner)
        self.write(directory / 'http/server.pid', str(pid))
        self.write(directory / 'http/binary.sha256', self.binary_sha)
        self.write(directory / 'http/CMakeCache.txt', 'synthetic actual build cache\n')
        self.write(directory / 'http/evalscope-version.txt', 'synthetic frozen version\n')
        for name in audit.TOOLS:
            self.write(directory / 'tools' / name, 'frozen ' + name)
        self.write(directory / 'http/run_acceptance.py', 'frozen run_acceptance.py')
        monitor = ['python', 'monitor.py']
        for flag, value in {'--model-dir': str(self.model),
                            '--pid-file': str(directory / 'http/server.pid'),
                            '--phase-file': str(directory / 'http/memory-phase.txt'),
                            '--cgroup-path': '/system.slice/' + unit,
                            '--ready-file': str(directory / 'memory/ready'),
                            '--stop-file': str(directory / 'memory/stop'),
                            '--out': str(directory / 'memory'), '--file-cache-mode': 'endpoints',
                            '--interval': '1.0', '--gpu-interval': '10.0'}.items():
            monitor += [flag, value]
        self.save(directory / 'monitor-command.json', monitor)
        self.save(directory / 'memory/memory-peak.json', {
            'root_pid': pid, 'root_start_ticks': 100, 'sampling_complete': True,
            'stop_reason': 'target_exited', 'prelaunch_sample_present': True,
            'existing_named_pids_at_start': [], 'target_sample_count': 100,
            'observation_policy': {'resource_interval_seconds': 1.0,
                                   'gpu_interval_seconds': 10.0, 'file_cache_mode': 'endpoints',
                                   'carry_forward_values': False},
            'model_file_cache_endpoint_observations': [
                {'phase': 'before_start', 'model_file_cache_status': 'ok', 't': start + .1},
                {'phase': 'after_exit', 'model_file_cache_status': 'ok', 't': start + 1898}],
            'resource_observations': {'binding_mismatch_samples': 0,
                                     'cgroup_paths': ['/sys/fs/cgroup/system.slice/' + unit]}})
        self.save(directory / 'cache-gate.json', {
            'cold_payload_established': True, 'payload_resident_bytes': 0, 'advice_errors': []})
        summaries = []
        for tier, n in enumerate(audit.LENGTHS):
            case = directory / f'http/context-{n}'
            prompt, tokens = f'prompt {n}', audit.target(n)
            first = (self.fixtures / f'context-{n}/requests.jsonl').read_text()
            self.write(directory / f'http/inputs/context-{n}.jsonl', first * 3)
            self.write(case / 'requests.jsonl', first * 3)
            rows, boundaries = [], []
            for i in range(3):
                request = case / f'run-{i + 1}'
                label = f'context-{n}:run{i + 1}'
                tick = start + 10 + (tier * 3 + i) * 100
                ttft = 10 if order == 0 else 9
                latency = ttft + (tokens - 1) / 10
                timing = {'start_monotonic_seconds': tick + 1,
                          'end_monotonic_seconds': tick + 1 + latency,
                          'within_client_boundaries': True}
                before = {'event': 'client_before', 'request_group': label,
                          'case': f'context-{n}', 'expected_requests': 1,
                          'requested_output': tokens, 'expected_input_min': n,
                          'expected_input_max': n, 'capacity': capacity,
                          'monotonic_seconds': tick, 'unix_seconds': tick}
                after = {'event': 'client_after', 'request_group': label,
                         'case': f'context-{n}', 'client_returncode': 0,
                         'monotonic_seconds': tick + 90, 'unix_seconds': tick + 90}
                row = {'success': 1, 'actual_input': n, 'actual_output': tokens,
                       'requested_max_tokens': tokens, 'finish': ['length'],
                       'request_stream': True, 'requested_capacity': audit.CAPACITY,
                       'effective_capacity': audit.CAPACITY, 'ttft': ttft, 'latency': latency,
                       'text': f'answer {n}', 'prompt_sha256': sha(prompt.encode()), 'timing': timing}
                rows.append(row)
                boundaries.append({'request_index': i + 1, 'request_group': label,
                                   'client_before': before, 'client_after': after,
                                   'http_timing': timing, 'capacity': capacity,
                                   **{k: row[k] for k in ('success', 'actual_input', 'actual_output',
                                                        'finish', 'requested_max_tokens')}})
                self.write(request / 'requests.jsonl', first)
                self.write(case / f'output-{i}.txt', row['text'])
                self.save(request / 'client-exit.json', {'returncode': 0, 'before': before, 'after': after})
                command = ['evalscope', 'perf', '--stream', '--no-test-connection', '--no-apply-chat-template']
                for flag, value in {'--model': 'qwen3.8-flash-next', '--api': 'openai',
                                    '--url': 'http://127.0.0.1:8172/v1/chat/completions',
                                    '--tokenizer-path': str(self.model), '--dataset': 'line_by_line',
                                    '--dataset-path': str(request / 'requests.jsonl'),
                                    '--min-prompt-length': str(n), '--max-prompt-length': str(n),
                                    '--max-tokens': str(tokens), '--temperature': '0', '--seed': '20260920',
                                    '--parallel': '1', '--number': '1', '--warmup-num': '0'}.items():
                    command += [flag, value]
                self.save(request / 'command.json', command)
            self.save(case / 'responses.json', rows)
            self.save(case / 'request-boundaries.json', boundaries)
            summaries.append({'length': n, 'outputs': [sha(f'answer {n}'.encode())] * 3,
                              'prompt_sha256': sha(prompt.encode()), 'deterministic': True,
                              'metrics': [{'ttft': r['ttft'], 'decode_tps':
                                           (tokens - 1) / (r['latency'] - r['ttft'])} for r in rows],
                              'performance_scope': 'six_tier', 'partial_performance_matrix': False})
        self.save(directory / 'http/results.json', summaries)

    def audit(self):
        return audit.audit_matrix(self.plan, self.off, self.on, self.pilot)

    def invalid(self, message):
        result = self.audit()
        self.assertEqual(result['decision'], 'INVALID_EVIDENCE', result)
        self.assertFalse(result['full_performance_screen_passed'])
        self.assertIn(message, result['failure'])
        self.assertIn(str(Path(audit.__file__).resolve()), result['source_sha256'])

    def test_complete_six_tiers_target257_and_distinct_references_pass(self):
        result = self.audit()
        self.assertEqual(result['decision'], 'PASS_FULL_PERFORMANCE_SCREEN', result)
        self.assertEqual(len(result['tiers']), 6)
        self.assertEqual(result['tiers'][-1]['output_tokens'], 257)
        self.assertAlmostEqual(result['tiers'][-1]['candidate']['decode_tps'][0], 10)
        self.assertIn(str(self.reference), result['source_sha256'])
        self.assertIn(str(self.off / 'http/results.json'), result['source_sha256'])
        self.assertFalse(result['performance_acceptance'])
        self.assertFalse(result['deployment_acceptance'])
        self.assertEqual(result['total_physical_RAM'], 'INDETERMINATE')

    def test_target256_is_not_target257(self):
        self.edit(self.on / 'http/context-261887/responses.json',
                  lambda rows: rows[0].update(actual_output=256, requested_max_tokens=256))
        self.invalid('raw response contract')

    def test_missing_tier(self):
        self.edit(self.on / 'http/results.json', lambda rows: rows.pop())
        self.invalid('missing/duplicate/out-of-order tier')

    def test_missing_repeat(self):
        self.edit(self.on / 'http/context-1024/responses.json', lambda rows: rows.pop())
        self.invalid('missing/extra raw requests')

    def test_duplicate_tier(self):
        self.edit(self.on / 'http/results.json', lambda rows: rows[1].update(length=1024))
        self.invalid('missing/duplicate/out-of-order tier')

    def test_cross_binary(self):
        self.edit(self.on / 'protocol.json', lambda p: p.update(binary_sha256='f' * 64))
        self.invalid('protocol binary/order/tiers')

    def test_replaced_binary_artifact(self):
        self.write(self.binary, 'replaced after the runs')
        self.invalid('source SHA mismatch')

    def test_build_cache_snapshot_mismatch(self):
        self.write(self.on / 'http/CMakeCache.txt', 'some unrelated build cache')
        self.invalid('source SHA mismatch')

    def test_cross_hot(self):
        self.edit(self.on / 'protocol.json',
                  lambda p: p['input_config_sha256'].update({str(self.hot): 'f' * 64}))
        self.invalid('model/hot identity')

    def test_partial_exit_rejected_before_reading_raw_or_monitor(self):
        self.edit(self.on / 'http/exit.json', lambda p: p.update(completed=5))
        (self.off / 'http/context-1024/responses.json').unlink()
        result = self.audit()
        self.assertIn('HTTP incomplete', result['failure'])
        self.assertFalse(any('/memory/' in p or p.endswith('/responses.json')
                             for p in result['source_sha256']))
        self.reuse_mock.assert_not_called()

    def test_missing_completion_rejected_before_reading_raw(self):
        (self.on / 'wrapper-exit.json').unlink()
        result = self.audit()
        self.assertEqual(result['decision'], 'INVALID_EVIDENCE')
        self.assertFalse(any(p.endswith('/responses.json') for p in result['source_sha256']))

    def test_summary_metric_mismatch(self):
        self.edit(self.on / 'http/results.json',
                  lambda rows: rows[0]['metrics'][0].update(decode_tps=999))
        self.invalid('summary does not match raw')

    def test_summary_output_hash_mismatch(self):
        self.edit(self.on / 'http/results.json',
                  lambda rows: rows[0]['outputs'].__setitem__(0, 'f' * 64))
        self.invalid('summary does not match raw')

    def test_non_success_response(self):
        self.edit(self.on / 'http/context-1024/responses.json',
                  lambda rows: rows[0].update(success=0))
        self.invalid('raw response contract')

    def test_eos_and_capacity_shrink_are_rejected(self):
        self.edit(self.on / 'http/context-1024/responses.json',
                  lambda rows: rows[0].update(finish=['stop']))
        self.invalid('raw response contract')
        self.edit(self.on / 'http/context-1024/responses.json',
                  lambda rows: rows[0].update(finish=['length'], effective_capacity={**audit.CAPACITY, 'max_len': 200000}))
        self.invalid('raw response contract')

    def test_wrong_reference_chain_even_with_equal_outputs(self):
        self.edit(self.on / 'runner-command.json',
                  lambda argv: argv.__setitem__(argv.index('--reference') + 1, str(self.reference)))
        self.invalid('runner command differs: --reference')

    def test_copied_tool_tamper(self):
        self.write(self.on / 'tools/run_acceptance.py', 'different tool')
        self.invalid('source SHA mismatch')

    def test_legacy_axis_accepts_full_new_dependency_set(self):
        self.tool_sha['offload_policy.py'] = sha(b'explicit helper dependency')
        for directory in (self.off, self.on):
            self.edit(directory / 'protocol.json',
                      lambda p: p.update(tool_sha256=self.tool_sha))
            self.write(directory / 'tools/offload_policy.py',
                       'explicit helper dependency')
        self.assertEqual(self.audit()['decision'], 'PASS_FULL_PERFORMANCE_SCREEN')

    def test_legacy_axis_rejects_extra_or_missing_tool_dependencies(self):
        path = self.on / 'protocol.json'
        original = path.read_text()
        for key, add in (('unexpected.py', True), ('run_acceptance.py', False)):
            def change(p):
                if add:
                    p['tool_sha256'][key] = 'a' * 64
                else:
                    p['tool_sha256'].pop(key)
            self.edit(path, change)
            self.invalid('incomplete tool source identities')
            path.write_text(original)

    def test_source_fixture_tamper(self):
        self.write(self.fixtures / 'context-1024/requests.jsonl', '{"prompt":"new"}\n')
        self.invalid('source SHA mismatch')

    def test_monitor_truncated_tail(self):
        self.edit(self.on / 'memory/memory-peak.json',
                  lambda p: p.update(sampling_complete=False, stop_reason='controller_stopped'))
        self.invalid('monitor identity/coverage incomplete')

    def test_runner_descendant_leak(self):
        self.edit(self.on / 'runner-process-group.json',
                  lambda p: p['after_cleanup'].update(live_pids=[999]))
        self.invalid('runner/client process group')

    def test_service_not_removed(self):
        self.edit(self.on / 'http/isolation/cleanup.json', lambda p: p.update(unit_removed=False))
        self.invalid('service cleanup failed')

    def test_cache_not_cold(self):
        self.edit(self.on / 'cache-gate.json', lambda p: p.update(payload_resident_bytes=4096))
        self.invalid('cold payload zero')

    def test_wrong_monitor_pid(self):
        self.edit(self.on / 'memory/memory-peak.json', lambda p: p.update(root_pid=999))
        self.invalid('monitor identity/coverage incomplete')

    def test_group_overlap(self):
        self.edit(self.on / 'wrapper-exit.json', lambda p: p.update(started_t=1000))
        self.invalid('ordering not established')

    def test_request_time_outside_client(self):
        self.edit(self.on / 'http/context-1024/responses.json',
                  lambda rows: rows[0]['timing'].update(start_monotonic_seconds=1))
        self.invalid('request boundaries differ')

    def test_client_pointed_at_another_service(self):
        self.edit(self.on / 'http/context-1024/run-1/command.json',
                  lambda argv: argv.__setitem__(argv.index('--url') + 1,
                                               'http://127.0.0.1:9999/v1/chat/completions'))
        self.invalid('client command differs: --url')

    def change_metric(self, index, *, ttft=9, decode=10):
        """Keep raw timing, boundaries and summary consistent for a real NO_GO."""
        case = self.on / 'http/context-261887'
        rows = json.loads((case / 'responses.json').read_text())
        row = rows[index]
        row['ttft'] = ttft
        row['latency'] = ttft + 256 / decode
        row['timing']['end_monotonic_seconds'] = (
            row['timing']['start_monotonic_seconds'] + row['latency'])
        self.save(case / 'responses.json', rows)
        self.edit(case / 'request-boundaries.json',
                  lambda values: values[index].update(http_timing=row['timing']))
        self.edit(self.on / 'http/results.json',
                  lambda values: values[-1]['metrics'].__setitem__(index, {
                      'ttft': ttft, 'decode_tps': 256 / (row['latency'] - ttft)}))

    def test_one_last_tier_failure_rejects_whole_matrix(self):
        # Each case uses consistent raw and summary metrics, not an evidence
        # corruption. Passing short tiers must not mask any last-tier failure.
        for index, values, failed in ((0, {'ttft': 10.1}, 'first_of_tier_ttft'),
                                      (1, {'ttft': 10.1}, 'later_ttft'),
                                      (2, {'decode': 9.9}, 'decode')):
            with self.subTest(failed=failed):
                self.change_metric(index, **values)
                result = self.audit()
                self.assertEqual(result['decision'], 'NO_GO_FULL_PERFORMANCE_SCREEN', result)
                self.assertTrue(result['evidence_contracts_passed'])
                self.assertFalse(result['full_performance_screen_passed'])
                self.assertTrue(all(t['passed'] for t in result['tiers'][:-1]))
                self.assertFalse(result['tiers'][-1]['checks'][failed])
                self.change_metric(index)


class MetricContracts(unittest.TestCase):
    def test_each_frozen_exit_and_equality(self):
        baseline = [{'ttft': 10, 'decode_tps': 8}, {'ttft': 9, 'decode_tps': 9},
                    {'ttft': 11, 'decode_tps': 10}]
        self.assertTrue(audit.screen_tier(baseline, copy.deepcopy(baseline))['passed'])
        for index, field, value, failed_key in (
                (0, 'ttft', 10.01, 'first_of_tier_ttft'),
                (1, 'ttft', 11.01, 'later_ttft'),
                (2, 'decode_tps', 7.99, 'decode')):
            with self.subTest(failed_key=failed_key):
                candidate = copy.deepcopy(baseline)
                candidate[index][field] = value
                result = audit.screen_tier(baseline, candidate)
                self.assertFalse(result['passed'])
                self.assertEqual([k for k, v in result['checks'].items() if not v], [failed_key])

    def test_later_uses_observed_max_not_pilot_min(self):
        base = [{'ttft': v, 'decode_tps': 8} for v in (10, 8, 12)]
        candidate = [{'ttft': v, 'decode_tps': 8} for v in (10, 11, 12)]
        self.assertTrue(audit.screen_tier(base, candidate)['passed'])

    def test_metric_nan_bool_and_extra_repeat_rejected(self):
        base = [{'ttft': 10, 'decode_tps': 8}] * 3
        for value in (float('nan'), float('inf'), True, 0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                audit.screen_tier(base, [{'ttft': value, 'decode_tps': 8}] * 3)
        with self.assertRaises(ValueError):
            audit.screen_tier(base, base + base[:1])


class EvidenceContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='matrix-source-contract-',
                                                    dir=audit.ROOT / '.q4t-work')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_source_change_during_audit_rejected(self):
        path = self.root / 'source.json'
        path.write_text('{}')
        evidence = audit.Evidence()
        evidence.json(path)
        path.write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError, 'source changed'):
            evidence.recheck()

    def test_pilot_requires_source_sha_before_recomputation(self):
        source = self.root / 'frozen.json'
        source.write_text('{}')
        decision = self.root / 'pilot-decision.json'
        decision.write_text(json.dumps({'decision': 'PASS_SCREENING', 'screening_passed': True,
            'evidence_contracts_passed': True, 'quality_11_passed': True,
            'source_sha256': {str(source): 'f' * 64}}))
        with mock.patch.object(audit.pilot, 'compare_evidence') as compare:
            with self.assertRaisesRegex(ValueError, 'source SHA mismatch'):
                audit.reuse_pilot(audit.Evidence(), decision)
            compare.assert_not_called()

    def test_forged_pilot_pass_rejected(self):
        source = self.root / 'frozen.json'
        source.write_text('{}')
        decision = self.root / 'pilot-decision.json'
        decision.write_text(json.dumps({'decision': 'PASS_SCREENING', 'screening_passed': True,
            'evidence_contracts_passed': True, 'quality_11_passed': True,
            'source_sha256': {str(source): sha(source.read_bytes())}}))
        with mock.patch.object(audit.pilot, 'compare_evidence', return_value={'decision': 'NO_GO'}):
            with self.assertRaisesRegex(ValueError, 'saved pilot decision differs'):
                audit.reuse_pilot(audit.Evidence(), decision)

    def test_existing_report_is_never_overwritten(self):
        output = self.root / 'existing-report.json'
        output.write_text('preserve earlier evidence\n')
        argv = ['audit', '--plan', 'unused', '--baseline', 'unused-off',
                '--candidate', 'unused-on', '--pilot-decision', 'unused-pilot',
                '--output', str(output)]
        with mock.patch('sys.argv', argv), mock.patch.object(audit, 'audit_matrix') as run:
            with self.assertRaises(FileExistsError):
                audit.main()
            run.assert_not_called()
        self.assertEqual(output.read_text(), 'preserve earlier evidence\n')


if __name__ == '__main__':
    unittest.main()
