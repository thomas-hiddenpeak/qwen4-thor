"""Host-only frozen chunk-order protocol contracts; no model or cache advice."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from compare_chunk_order import CAPACITY, compare_evidence, screen_metrics
from run_budget_experiment import (experiment_environment, monitor_command,
                                   runner_command, selected_performance_plan)

ROOT = Path(__file__).resolve().parents[2]


class WrapperProtocolTests(unittest.TestCase):
    def args(self):
        return SimpleNamespace(mode='performance', binary=Path('build/q4t'),
            model_dir=Path('/read-only/model'), fixtures=Path('.q4t-work/fixtures'),
            hot_list=Path('.q4t-work/hot.json'), port=8190, reference=None,
            host_cache_max_bytes=16 << 30, monitor_interval=1, gpu_interval=10,
            file_cache_mode='endpoints')

    def test_default_is_three_single_tier_partial(self):
        p = selected_performance_plan('performance')
        self.assertEqual(p['lengths'], [45056])
        self.assertEqual(p['repeats'], 3)
        self.assertTrue(p['partial'])
        self.assertTrue(p['partial_offload_matrix'])
        self.assertFalse(p['performance_acceptance'])

    def test_five_tiers_are_still_incomplete_offload(self):
        p = selected_performance_plan('performance', '1024,4096,8192,45056,204800')
        self.assertEqual(p['scope'], 'five_tier')
        self.assertFalse(p['partial'])
        self.assertTrue(p['partial_offload_matrix'])

    def test_six_tiers_target_uses_257_and_does_not_accept_performance(self):
        p = selected_performance_plan('performance', '1024,4096,8192,45056,204800,261887')
        self.assertEqual(p['scope'], 'six_tier')
        self.assertFalse(p['partial_offload_matrix'])
        self.assertEqual(p['requests'][-1], {'input_tokens': 261887,
            'max_tokens': 257, 'total_tokens': 262144, 'repeats': 3})
        self.assertFalse(p['performance_acceptance'])
        command = runner_command(self.args(), Path('.q4t-work/out'), 'q4t-owned.service', p)
        self.assertEqual(command[command.index('--extra-lengths')+1], '261887')
        self.assertEqual(command[command.index('--target-total')+1], '262144')

    def test_invalid_selection_and_capacities_rejected(self):
        for lengths, repeats, total in [('45056,45056', 3, 0), ('', 3, 0),
                ('262144', 3, 0), ('261887', 3, 261887), ('45056', 3, 262144),
                ('45056', 2, 0), ('45056', 4, 0)]:
            with self.subTest(lengths=lengths, repeats=repeats, total=total):
                with self.assertRaises(ValueError):
                    selected_performance_plan('performance', lengths, repeats, total)

    def test_quality_unchanged_and_rejects_performance_options(self):
        self.assertIsNone(selected_performance_plan('quality'))
        with self.assertRaises(ValueError):
            selected_performance_plan('quality', '45056')

    def test_explicit_env_overrides_inherited_experiments(self):
        env = experiment_environment(1, {'PATH': '/bin', 'Q4T_FP8_TEST': '1',
                                         'Q4T_MOE_CHUNK_ORDER': '0'})
        self.assertEqual(env['Q4T_MOE_CHUNK_ORDER'], '1')
        self.assertEqual(env['Q4T_MOE_EVICT_WEIGHT'], '0')
        self.assertNotIn('Q4T_FP8_TEST', env)
        self.assertEqual(env['PATH'], '/bin')
        self.assertEqual(experiment_environment(0, env)['Q4T_MOE_CHUNK_ORDER'], '0')

    def test_monitor_keeps_exact_pid_file_and_fixed_sampling(self):
        command = monitor_command(self.args(), Path('.q4t-work/out'), 'q4t-owned.service')
        for flag, value in [('--interval', '1'), ('--gpu-interval', '10'),
                ('--file-cache-mode', 'endpoints'),
                ('--pid-file', '.q4t-work/out/http/server.pid'),
                ('--cgroup-path', '/system.slice/q4t-owned.service'),
                ('--model-dir', '/read-only/model')]:
            self.assertEqual(command[command.index(flag)+1], value)


class ScreeningTests(unittest.TestCase):
    base = [{'ttft': 220., 'decode_tps': 7.2},
            {'ttft': 222., 'decode_tps': 7.3},
            {'ttft': 224., 'decode_tps': 7.25}]
    better = [{'ttft': 219., 'decode_tps': 7.3},
              {'ttft': 220., 'decode_tps': 7.2},
              {'ttft': 221., 'decode_tps': 7.25}]

    def test_frozen_screen_pass_is_not_full_acceptance(self):
        result = screen_metrics(self.base, self.better)
        self.assertTrue(result['screening_passed'])
        self.assertFalse(result['performance_acceptance'])
        self.assertFalse(result['warm_ttft_ranges_overlap'])

    def test_overlap_despite_mean_improvement_is_no_go(self):
        candidate = copy.deepcopy(self.better)
        candidate[2]['ttft'] = 222.
        result = screen_metrics(self.base, candidate)
        self.assertEqual(result['decision'], 'NO_GO')
        self.assertTrue(result['warm_ttft_ranges_overlap'])

    def test_cold_regression_is_no_go(self):
        candidate = copy.deepcopy(self.better)
        candidate[0]['ttft'] = self.base[0]['ttft']
        self.assertFalse(screen_metrics(self.base, candidate)['screening_passed'])

    def test_decode_below_observed_min_is_no_go(self):
        candidate = copy.deepcopy(self.better)
        candidate[0]['decode_tps'] = 7.199
        self.assertFalse(screen_metrics(self.base, candidate)['screening_passed'])

    def test_no_missing_extra_or_nonfinite_samples(self):
        for candidate in [self.better[:2], self.better*2,
                          [{'ttft': float('nan'), 'decode_tps': 8.}]*3]:
            with self.assertRaises(ValueError):
                screen_metrics(self.base, candidate)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        work = ROOT / 'build/chunk-order-protocol-contracts'
        work.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.quality, self.base, self.on = [self.root / x for x in ('quality', 'off', 'on')]
        for directory, mode, order, start in [(self.quality, 'quality', 1, 0),
                (self.base, 'performance', 0, 10), (self.on, 'performance', 1, 20)]:
            p = dict(mode=mode, chunk_order=order, binary_sha256='a'*64,
                effective_environment=experiment_environment(order, {}),
                host_cache_max_bytes=(None if mode=='quality' else 16<<30),
                swap_max_bytes=0, lengths=[45056], repeats=3,
                performance_plan=selected_performance_plan(mode),
                monitor=dict(interval_seconds=1,gpu_interval_seconds=10,file_cache_mode='endpoints'),
                tool_sha256={'monitor_memory.py':'frozen'}, fixture_sha256={'requests.jsonl':'same'},
                model_files=[{'path':'model','size':123}], **CAPACITY,
                request_deadline_ms=1800000,client_lifecycle='per request')
            self.write(directory,'protocol.json',p)
            self.write(directory,'wrapper-exit.json',dict(runner_rc=0,monitor_rc=0,
                failure=None,cleanup_failed=False,unit_after_cleanup={'LoadState':'not-found'},
                started_t=start,ended_t=start+5))
            self.write(directory,'http/exit.json',dict(server=0,http_output_checks_passed=True,
                failure=None,cleanup_failure=None,completed=11 if mode=='quality' else 1,
                partial_performance_matrix=mode=='performance',full_offload_matrix_completed=False))
            self.write(directory,'http/capacity.json',dict(matches_requested=True,
                requested=CAPACITY,effective=CAPACITY))
            self.write(directory,'http/server-command.json',dict(effective_q4t_environment=p['effective_environment'],
                isolation=dict(host_cache_max_bytes=p['host_cache_max_bytes'],swap_max_bytes=0)))
            (directory/'http/binary.sha256').write_text('a'*64)
            if mode=='quality':
                self.write(directory,'http/results.json',[dict(id=str(i),success=1,
                    exact_match=True,length_match=True,finish=['stop'],
                    requested_capacity=CAPACITY,effective_capacity=CAPACITY) for i in range(11)])
                continue
            self.write(directory,'cache-gate.json',dict(cold_payload_established=True,
                                                       payload_resident_bytes=0,advice_errors=[]))
            metrics = ScreeningTests.base if order==0 else ScreeningTests.better
            rows=[dict(success=1,actual_input=45056,actual_output=256,requested_max_tokens=256,
                finish=['length'],requested_capacity=CAPACITY,effective_capacity=CAPACITY,
                text='identical output',prompt_sha256='prompt',ttft=r['ttft'],
                latency=r['ttft']+255/r['decode_tps']) for r in metrics]
            metrics=[dict(ttft=r['ttft'],decode_tps=255/(r['latency']-r['ttft'])) for r in rows]
            self.write(directory,'http/context-45056/responses.json',rows)
            self.write(directory,'http/results.json',[dict(length=45056,
                outputs=[hashlib.sha256(r['text'].encode()).hexdigest() for r in rows],
                prompt_sha256='prompt',metrics=metrics)])

    def write(self,directory,name,data):
        p=directory/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data))

    def alter(self,directory,name,key,value):
        d=json.loads((directory/name).read_text());d[key]=value;self.write(directory,name,d)

    def test_complete_fair_pair_is_accepted_only_for_screening(self):
        result=compare_evidence(self.base,self.on,self.quality)
        self.assertEqual(result['decision'],'PASS_SCREENING')
        self.assertFalse(result['performance_acceptance'])

    def test_different_monitor_or_binary_rejected(self):
        self.alter(self.on,'protocol.json','monitor',dict(interval_seconds=1,
            gpu_interval_seconds=10,file_cache_mode='every_sample'))
        with self.assertRaises(ValueError):compare_evidence(self.base,self.on,self.quality)

    def test_missing_exit_not_interpreted_as_zero(self):
        (self.on/'http/exit.json').unlink()
        with self.assertRaises(OSError):compare_evidence(self.base,self.on,self.quality)

    def test_failed_quality_blocks_screening(self):
        self.alter(self.quality,'http/exit.json','http_output_checks_passed',False)
        with self.assertRaises(ValueError):compare_evidence(self.base,self.on,self.quality)

    def test_output_shortfall_blocks_metrics_comparison(self):
        p=self.on/'http/context-45056/responses.json';rows=json.loads(p.read_text())
        rows[0]['actual_output']=255;p.write_text(json.dumps(rows))
        with self.assertRaises(ValueError):compare_evidence(self.base,self.on,self.quality)


if __name__ == '__main__':
    unittest.main()
