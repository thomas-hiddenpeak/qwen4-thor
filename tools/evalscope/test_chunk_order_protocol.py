"""Host-only frozen chunk-order protocol contracts; no model or cache advice."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from compare_chunk_order import (CAPACITY, FROZEN_CONFIG_SHA256,
    FROZEN_QUALITY_SHA256, HOT, MODEL, QUALITY_CASES, QUALITY_FIXTURES,
    QUALITY_REFERENCE, PERF_FIXTURES, PERF_FIXTURE_SHA256, PERF_REFERENCE,
    PERF_REFERENCE_SHA256, compare_evidence, screen_metrics)
from run_budget_experiment import (experiment_environment, monitor_command,
    process_group_state, run_owned_runner, runner_command,
    selected_performance_plan)

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


class OwnedRunnerTests(unittest.TestCase):
    def setUp(self):
        work=ROOT/'build/chunk-order-process-contracts'
        work.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        self.out=Path(self.temp.name)

    def run_child(self, source, *, timeout=5):
        with (self.out/'runner.log').open('x') as log:
            return run_owned_runner([sys.executable,'-c',source],cwd=ROOT,
                env=os.environ.copy(),log=log,timeout=timeout,
                evidence=self.out/'group.json',term_grace=.2,kill_grace=2)

    def test_normal_exit_records_reaped_empty_owned_group(self):
        self.assertEqual(self.run_child('pass'),0)
        record=json.loads((self.out/'group.json').read_text())
        self.assertTrue(record['cleanup_complete'])
        self.assertTrue(record['runner_reaped'])
        self.assertNotEqual(record['pgid'],os.getpgrp())
        self.assertEqual(record['after_cleanup']['live_pids'],[])
        self.assertEqual(record['signals'],[])

    def test_nonzero_child_exit_is_preserved(self):
        self.assertEqual(self.run_child('raise SystemExit(7)'),7)
        record=json.loads((self.out/'group.json').read_text())
        self.assertEqual(record['returncode'],7)
        self.assertTrue(record['cleanup_complete'])

    def test_timeout_kills_sleeping_descendant_but_not_external_process(self):
        external=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],
                                  start_new_session=True)
        child_pid=self.out/'descendant.pid'
        source=("import signal,subprocess,sys,time\n"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                f"open({str(child_pid)!r},'w').write(str(child.pid))\n"
                "time.sleep(60)\n")
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.run_child(source,timeout=2)
            self.assertTrue(child_pid.is_file(),'real sleeping descendant was started')
            record=json.loads((self.out/'group.json').read_text())
            self.assertEqual([x['signal'] for x in record['signals']],['SIGTERM','SIGKILL'])
            self.assertIn(int(child_pid.read_text()),record['before_cleanup']['live_pids'])
            self.assertTrue(record['cleanup_complete'])
            self.assertTrue(record['runner_reaped'])
            self.assertEqual(process_group_state(record['pgid'])['live_pids'],[])
            self.assertIsNone(external.poll(),'unrelated process was not signalled')
        finally:
            external.terminate()
            external.wait(timeout=5)


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
                input_config_sha256=dict(FROZEN_CONFIG_SHA256),
                model_files=[{'path':'model','size':123}], **CAPACITY,
                request_deadline_ms=1800000,client_lifecycle='per request')
            if mode == 'quality':
                p['fixtures'] = str(QUALITY_FIXTURES)
                p['input_config_sha256'].update(FROZEN_QUALITY_SHA256)
                p['fixture_sha256'] = {Path(path).name: digest
                    for path,digest in FROZEN_QUALITY_SHA256.items()
                    if Path(path).parent == QUALITY_FIXTURES}
            else:
                reference = PERF_REFERENCE if order == 0 else self.base/'http/results.json'
                reference_sha = PERF_REFERENCE_SHA256 if order == 0 else hashlib.sha256(reference.read_bytes()).hexdigest()
                p['fixtures'] = str(PERF_FIXTURES)
                p['fixture_sha256'] = {'context-45056/requests.jsonl': PERF_FIXTURE_SHA256}
                p['input_config_sha256'].update({str(reference): reference_sha,
                    str(PERF_FIXTURES/'context-45056/requests.jsonl'): PERF_FIXTURE_SHA256})
                self.write(directory,'runner-command.json',
                    ['python','runner.py','--reference',str(reference)])
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
                argv=['binary','serve','--model-dir',str(MODEL),
                    '--moe-hot-list',str(HOT),'--moe-resident-slots','256'],
                hot_list=dict(path=str(HOT),sha256=FROZEN_CONFIG_SHA256[str(HOT)]),
                isolation=dict(host_cache_max_bytes=p['host_cache_max_bytes'],swap_max_bytes=0)))
            (directory/'http/binary.sha256').write_text('a'*64)
            if mode=='quality':
                self.write(directory,'runner-command.json',
                    ['python','runner.py','--reference',str(QUALITY_REFERENCE)])
                self.write(directory,'http/results.json',[dict(id=id,success=1,
                    actual_input=length,prompt_sha256=prompt,text=text,actual_output=output,
                    exact_match=True,length_match=True,finish=['stop'],
                    requested_capacity=CAPACITY,effective_capacity=CAPACITY)
                    for id,length,prompt,text,output in QUALITY_CASES])
                continue
            self.write(directory,'cache-gate.json',dict(cold_payload_established=True,
                                                       payload_resident_bytes=0,advice_errors=[]))
            self.write(directory,'runner-process-group.json',dict(cleanup_complete=True,
                runner_reaped=True,failure=None,after_cleanup=dict(live_pids=[],errors=[])))
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

    def test_unknown_client_cleanup_blocks_screening(self):
        self.alter(self.on,'runner-process-group.json','cleanup_complete',False)
        with self.assertRaisesRegex(ValueError,'process group'):
            compare_evidence(self.base,self.on,self.quality)

    def test_failed_quality_blocks_screening(self):
        self.alter(self.quality,'http/exit.json','http_output_checks_passed',False)
        with self.assertRaises(ValueError):compare_evidence(self.base,self.on,self.quality)

    def test_output_shortfall_blocks_metrics_comparison(self):
        p=self.on/'http/context-45056/responses.json';rows=json.loads(p.read_text())
        rows[0]['actual_output']=255;p.write_text(json.dumps(rows))
        with self.assertRaises(ValueError):compare_evidence(self.base,self.on,self.quality)

    def test_replaced_hot_hash_rejected_even_if_pair_agrees(self):
        for directory in (self.base,self.on):
            p=json.loads((directory/'protocol.json').read_text())
            p['input_config_sha256'][str(HOT)]='b'*64
            self.write(directory,'protocol.json',p)
        with self.assertRaisesRegex(ValueError,'config/index/hot'):
            compare_evidence(self.base,self.on,self.quality)

    def test_actual_server_hot_hash_must_match_protocol(self):
        self.alter(self.on,'http/server-command.json','hot_list',
            dict(path=str(HOT),sha256='b'*64))
        with self.assertRaisesRegex(ValueError,'actual server hot'):
            compare_evidence(self.base,self.on,self.quality)

    def test_model_config_hash_change_rejected(self):
        p=json.loads((self.on/'protocol.json').read_text())
        p['input_config_sha256'][str(MODEL/'config.json')]='b'*64
        self.write(self.on,'protocol.json',p)
        with self.assertRaisesRegex(ValueError,'config/index/hot'):
            compare_evidence(self.base,self.on,self.quality)

    def test_another_eleven_successful_questions_are_rejected(self):
        path=self.quality/'http/results.json';rows=json.loads(path.read_text())
        for row in rows:row['id']='other-'+row['id']
        path.write_text(json.dumps(rows))
        with self.assertRaisesRegex(ValueError,'IDs/prompt/text/token'):
            compare_evidence(self.base,self.on,self.quality)

    def test_quality_changed_prompt_rejected_despite_success_flags(self):
        path=self.quality/'http/results.json';rows=json.loads(path.read_text())
        rows[0]['prompt_sha256']='b'*64;path.write_text(json.dumps(rows))
        with self.assertRaisesRegex(ValueError,'IDs/prompt/text/token'):
            compare_evidence(self.base,self.on,self.quality)

    def test_quality_changed_text_rejected_despite_exact_flag(self):
        path=self.quality/'http/results.json';rows=json.loads(path.read_text())
        rows[0]['text']+='\n';path.write_text(json.dumps(rows))
        with self.assertRaisesRegex(ValueError,'IDs/prompt/text/token'):
            compare_evidence(self.base,self.on,self.quality)

    def test_quality_reference_not_passed_rejected(self):
        self.write(self.quality,'runner-command.json',['python','runner.py'])
        with self.assertRaisesRegex(ValueError,'reference was not passed'):
            compare_evidence(self.base,self.on,self.quality)

    def test_quality_manifest_hash_change_rejected(self):
        p=json.loads((self.quality/'protocol.json').read_text())
        p['input_config_sha256'][str(QUALITY_FIXTURES/'manifest.json')]='b'*64
        self.write(self.quality,'protocol.json',p)
        with self.assertRaisesRegex(ValueError,'fixture/reference'):
            compare_evidence(self.base,self.on,self.quality)

    def test_candidate_reference_must_point_to_this_baseline(self):
        self.write(self.on,'runner-command.json',
            ['python','runner.py','--reference',str(PERF_REFERENCE)])
        with self.assertRaisesRegex(ValueError,'reference path/hash'):
            compare_evidence(self.base,self.on,self.quality)

    def test_candidate_reference_digest_must_match_this_baseline(self):
        p=json.loads((self.on/'protocol.json').read_text())
        p['input_config_sha256'][str(self.base/'http/results.json')]='b'*64
        self.write(self.on,'protocol.json',p)
        with self.assertRaisesRegex(ValueError,'reference path/hash'):
            compare_evidence(self.base,self.on,self.quality)


if __name__ == '__main__':
    unittest.main()
