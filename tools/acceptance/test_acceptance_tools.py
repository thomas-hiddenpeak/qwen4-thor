"""Host-only failure/identity contracts; never starts q4t or touches caches."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / 'tools/acceptance'
TIERS = (1024, 4096, 8192, 45056, 204800, 261887)


class ComparisonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='acceptance-host-',
                                              dir=ROOT / '.q4t-work')
        self.work = Path(self.tmp.name)
        self.summary = []
        for tag in ('base', 'cand'):
            summary = []
            for tier in TIERS:
                count = 257 if tier == 261887 else 256
                rate = 10.0 if tag == 'base' else 6.0
                rows = [dict(success=1, actual_input=tier, actual_output=count,
                             finish=['length'], text='identical text',
                             prompt_sha256='a' * 64, ttft=1.0,
                             latency=1 + (count - 1) / rate)
                        for _ in range(3)]
                case = self.work / f'e2e-{tag}' / f'context-{tier}'
                case.mkdir(parents=True)
                (case / 'responses.json').write_text(json.dumps(rows))
                summary.append(dict(length=tier,
                    outputs=[hashlib.sha256(r['text'].encode()).hexdigest()
                             for r in rows], prompt_sha256='a' * 64,
                    deterministic=True,
                    metrics=[dict(ttft=1.0, decode_tps=rate) for _ in rows]))
            (self.work / f'e2e-{tag}/results.json').write_text(json.dumps(summary))
            (self.work / f'e2e-{tag}/exit.json').write_text(json.dumps(dict(
                server=0, http_output_checks_passed=True, failure=None, completed=6)))

    def tearDown(self):
        self.tmp.cleanup()

    def compare(self, expected):
        run = subprocess.run([sys.executable, '-B', str(TOOLS / 'compare_e2e.py'),
                              'base', 'cand', '--work-dir', str(self.work)],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, expected, run.stdout + run.stderr)
        return run

    def mutate(self, relative, change):
        path = self.work / relative
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data))

    def test_complete_pass(self):
        self.compare(0)

    def test_failed_server_exit_rejects_successful_rows(self):
        self.mutate('e2e-cand/exit.json', lambda d: d.update(server=-9))
        self.compare(1)

    def test_legacy_exit_supported_but_missing_exit_rejected(self):
        path = self.work / 'e2e-cand/exit.json'
        path.write_text('runner_rc=0\ntag=cand\ndate=legacy\n')
        self.compare(0)
        path.unlink()
        self.compare(1)

    def test_missing_tier_fails(self):
        self.mutate('e2e-cand/results.json', lambda d: d.pop())
        self.compare(1)

    def test_duplicate_tier_fails(self):
        self.mutate('e2e-cand/results.json', lambda d: d.append(copy.deepcopy(d[0])))
        self.compare(1)

    def test_incomplete_repeats_fail(self):
        self.mutate('e2e-cand/context-1024/responses.json', lambda d: d.pop())
        self.compare(1)

    def test_output_difference_fails_even_when_hashes_agree(self):
        self.mutate('e2e-cand/context-1024/responses.json',
                    lambda d: [r.update(text='other text') for r in d])
        digest = hashlib.sha256(b'other text').hexdigest()
        self.mutate('e2e-cand/results.json',
                    lambda d: d[0].update(outputs=[digest] * 3))
        self.compare(1)

    def test_target_contract_failure(self):
        self.mutate('e2e-cand/context-261887/responses.json',
                    lambda d: d[0].update(actual_output=256))
        self.compare(1)

    def test_finish_and_http_failures(self):
        for field, value in [('finish', ['stop']), ('success', 0)]:
            with self.subTest(field=field):
                p = 'e2e-cand/context-1024/responses.json'
                original = (self.work / p).read_text()
                self.mutate(p, lambda d: d[0].update({field: value}))
                self.compare(1)
                (self.work / p).write_text(original)

    def test_prompt_mismatch(self):
        self.mutate('e2e-cand/context-1024/responses.json',
                    lambda d: [r.update(prompt_sha256='b' * 64) for r in d])
        self.mutate('e2e-cand/results.json',
                    lambda d: d[0].update(prompt_sha256='b' * 64))
        self.compare(1)

    def test_invalid_or_forged_metrics(self):
        for value in (None, 0, -1, float('nan'), float('inf'), True, 99.0):
            with self.subTest(value=value):
                p = 'e2e-cand/results.json'
                original = (self.work / p).read_text()
                self.mutate(p, lambda d: d[0]['metrics'][0].update(decode_tps=value))
                self.compare(1)
                (self.work / p).write_text(original)

    def test_performance_failure(self):
        self.mutate('e2e-cand/context-1024/responses.json',
                    lambda d: [r.update(latency=1 + 255 / 4) for r in d])
        self.mutate('e2e-cand/results.json',
                    lambda d: d[0].update(metrics=[dict(ttft=1, decode_tps=4)] * 3))
        self.compare(1)

    def test_missing_and_malformed_raw_evidence(self):
        p = self.work / 'e2e-cand/context-1024/responses.json'
        p.write_text('{broken')
        self.compare(1)
        p.unlink()
        self.compare(1)


class WrapperTest(unittest.TestCase):
    """Run real shell wrappers with fake monitor/client/server processes."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='acceptance-wrapper-',
                                              dir=ROOT / '.q4t-work')
        self.work = Path(self.tmp.name)
        self.fake = self.work / 'fake-tools'
        self.fake.mkdir()
        self.env = dict(os.environ, PATH=f'{self.fake}:{os.environ["PATH"]}',
                        Q4T_ACCEPTANCE_WORK_DIR=str(self.work),
                        REAL_PYTHON=sys.executable, PYTHONDONTWRITEBYTECODE='1')
        # Dispatch only known test tool paths. Other Python snippets run normally.
        dispatch = '''#!{python}
import json, os, pathlib, sys, time
args = sys.argv[1:]
def value(flag): return args[args.index(flag) + 1]
if args and args[0].endswith('/monitor_memory.py'):
    out = pathlib.Path(value('--out'))
    probe = out.parent.name.startswith('c3-')
    (out / 'received-argv.json').write_text(json.dumps(args))
    if probe and 'FAKE_PROBE_MONITOR_START_RC' in os.environ:
        sys.exit(int(os.environ['FAKE_PROBE_MONITOR_START_RC']))
    pid_file = pathlib.Path(value('--pid-file'))
    phase_file = pathlib.Path(value('--phase-file'))
    observations = {{'pid_before_ready': pid_file.exists(), 'pids': [], 'phases': []}}
    if not (probe and os.environ.get('FAKE_PROBE_NO_READY')):
        pathlib.Path(value('--ready-file')).touch()
    while True:
        if pid_file.exists():
            pid = int(pid_file.read_text())
            if pid not in observations['pids']: observations['pids'].append(pid)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                if not os.environ.get('FAKE_MONITOR_WAIT_FOR_STOP'):
                    observations['natural_exit'] = True
                    break
        if phase_file.exists():
            phase = phase_file.read_text().strip()
            if phase not in observations['phases']:
                observations['phases'].append(phase)
        if pathlib.Path(value('--stop-file')).exists():
            observations['natural_exit'] = False
            break
        time.sleep(.005)
    (out / 'observations.json').write_text(json.dumps(observations))
    (out / 'memory.csv').write_text('t,root\\n0,0\\n')
    (out / 'memory-peak.json').write_text('{{"test_monitor": true}}')
    sys.exit(int(os.environ.get('FAKE_PROBE_MONITOR_RC', '0')) if probe else 0)
if args and args[0].endswith('/run_acceptance.py'):
    out = pathlib.Path(value('--output')); out.mkdir()
    (out / 'server.pid').write_text(str(os.getpid()))
    (out / 'received-argv.json').write_text(json.dumps(args))
    (out / 'exit.json').write_text('{{"http_output_checks_passed": false, "failure": "synthetic"}}')
    time.sleep(.03)
    sys.exit(int(os.environ.get('FAKE_RUNNER_RC', '0')))
if args and args[0].endswith('/compare_e2e.py') and 'FAKE_COMPARE_RC' in os.environ:
    print('synthetic comparison')
    sys.exit(int(os.environ['FAKE_COMPARE_RC']))
if args and args[0].endswith('/memory_accounting.py') and 'FAKE_MEMORY_RC' in os.environ:
    stage = 'probe' if '/c3-' in value('--memory-dir') else 'matrix'
    output = pathlib.Path(value('--output'))
    output.with_suffix('.argv.json').write_text(json.dumps(args))
    rc = int(os.environ.get('FAKE_MEMORY_' + stage.upper() + '_RC',
                            os.environ['FAKE_MEMORY_RC']))
    if os.environ.get('FAKE_MEMORY_MISSING_STAGE') != stage:
        output.write_text(json.dumps({{'gate': 'PASS' if rc == 0 else 'FAIL'}}))
    if os.environ.get('FAKE_MEMORY_DROP_SOURCE_STAGE') == stage:
        (pathlib.Path(value('--memory-dir')) / 'memory.csv').unlink()
    sys.exit(rc)
if args and args[0] == '-' and len(args) > 1 and args[1] == '8151':
    sys.stdin.read()
    time.sleep(.06)
    print('{{"finish":"length","in":45056,"out":8}}')
    sys.exit(int(os.environ.get('FAKE_WARMUP_RC', '0')))
os.execv(os.environ['REAL_PYTHON'], [os.environ['REAL_PYTHON']] + args)
'''.format(python=sys.executable)
        self.executable(self.fake / 'python3', dispatch)
        self.executable(self.fake / 'sudo', '#!/bin/sh\nexit 0\n')
        self.executable(self.fake / 'pgrep', '#!/bin/sh\nexit 1\n')
        self.executable(self.fake / 'sleep', '#!/bin/sh\n/bin/sleep 0.02\n')
        self.binary = self.work / 'selected-binary'
        self.executable(self.binary, f'#!{sys.executable}\n' + '''
import json, os, pathlib, signal, sys, time
out = pathlib.Path(os.readlink('/proc/self/fd/1')).parent
launch = {'pid': os.getpid(), 'ready_present': (out / 'memory/ready').exists(),
          'phase': (out / 'memory-phase.txt').read_text().strip()}
(out / 'server-launch.json').write_text(json.dumps(launch))
def stop(signum, frame):
    time.sleep(.06)
    sys.exit(int(os.environ.get('FAKE_PROBE_EXIT_RC', '0')))
signal.signal(signal.SIGTERM, stop)
time.sleep(.06)
print('serving on port', flush=True)
signal.pause()
''')
        self.env['Q4T_ACCEPTANCE_BINARY'] = str(self.binary)
        self.fixture = self.work / 'fixtures'
        (self.fixture / 'context-45056').mkdir(parents=True)
        (self.fixture / 'context-45056/requests.jsonl').write_text('{"prompt":"fake"}\n')
        self.env['Q4T_ACCEPTANCE_FIXTURES'] = str(self.fixture)

    def executable(self, path, text):
        path.write_text(text)
        path.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, script, *args):
        return subprocess.run(['bash', str(TOOLS / script), *args], env=self.env,
                              capture_output=True, text=True, timeout=15)

    def test_wrapper_binary_and_structured_exit_preserved(self):
        run = self.invoke('run-e2e.sh', 'fresh')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        out = self.work / 'e2e-fresh'
        argv = json.loads((out / 'received-argv.json').read_text())
        self.assertEqual(argv[argv.index('--binary') + 1], str(self.binary))
        self.assertEqual(json.loads((out / 'exit.json').read_text())['failure'], 'synthetic')
        self.assertEqual(json.loads((out / 'wrapper-exit.json').read_text())['runner_rc'], 0)
        self.assertTrue((out / 'memory/memory-peak.json').is_file())
        monitor = json.loads((out / 'memory/received-argv.json').read_text())
        self.assertIn('--model-dir', monitor)
        self.assertTrue(monitor[monitor.index('--model-dir') + 1].endswith(
            'Qwen3.8-Flash-Next-NVFP4-SSD-Stream'))

    def test_wrapper_propagates_failure(self):
        self.env['FAKE_RUNNER_RC'] = '7'
        run = self.invoke('run-e2e.sh', 'failure')
        self.assertEqual(run.returncode, 7, run.stdout + run.stderr)
        self.assertTrue((self.work / 'e2e-failure/exit.json').is_file())

    def test_wrapper_monitor_natural_exit_and_bounded_fallback(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                if fallback:
                    self.env['FAKE_MONITOR_WAIT_FOR_STOP'] = '1'
                tag = f'drain-{int(fallback)}'
                run = self.invoke('run-e2e.sh', tag)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                out = self.work / f'e2e-{tag}'
                status = json.loads((out / 'wrapper-exit.json').read_text())
                expected = 'natural_exit_timeout' if fallback else 'natural_exit'
                self.assertEqual(status['monitor_stop_reason'], expected)
                observations = json.loads((out / 'memory/observations.json').read_text())
                self.assertEqual(observations['natural_exit'], not fallback)

    def test_wrapper_refuses_existing_evidence(self):
        out = self.work / 'e2e-existing'
        out.mkdir(); (out / 'sentinel').write_text('keep')
        run = self.invoke('run-e2e.sh', 'existing')
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual((out / 'sentinel').read_text(), 'keep')

    def test_c3_passes_selected_binary_to_matrix(self):
        run = self.invoke('c3-pagecache-protocol.sh', 'protocol', '0', 'none',
                          '0', str(self.binary), '--acceptance')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        out = self.work / 'e2e-protocol'
        argv = json.loads((out / 'received-argv.json').read_text())
        self.assertEqual(argv[argv.index('--binary') + 1], str(self.binary))
        probe = self.work / 'c3-protocol'
        self.assertTrue((probe / 'memory/memory-peak.json').is_file())
        original = (probe / 'memory/memory-peak.json').read_bytes()
        run = self.invoke('c3-pagecache-protocol.sh', 'protocol', '0', 'none',
                          '0', str(self.binary), '--acceptance')
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual((probe / 'memory/memory-peak.json').read_bytes(), original)

    def test_c3_monitor_prelaunch_pid_model_and_phases(self):
        self.env['Q4T_MODEL_DIR'] = str(self.work / 'selected-model')
        run = self.invoke('c3-pagecache-protocol.sh', 'lifecycle', '0', 'none',
                          '0', str(self.binary))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        out = self.work / 'c3-lifecycle'
        launch = json.loads((out / 'server-launch.json').read_text())
        self.assertTrue(launch['ready_present'])
        self.assertEqual(launch['phase'], 'startup')
        self.assertEqual(int((out / 'server.pid').read_text()), launch['pid'])
        monitor = json.loads((out / 'memory/received-argv.json').read_text())
        for flag, expected in [('--model-dir', self.env['Q4T_MODEL_DIR']),
                               ('--pid-file', str(out / 'server.pid')),
                               ('--phase-file', str(out / 'memory-phase.txt'))]:
            self.assertEqual(monitor[monitor.index(flag) + 1], expected)
        observations = json.loads((out / 'memory/observations.json').read_text())
        self.assertFalse(observations['pid_before_ready'])
        self.assertEqual(observations['pids'], [launch['pid']])
        self.assertEqual(observations['phases'], ['startup', 'warmup', 'shutdown'])
        self.assertTrue(observations['natural_exit'])
        self.assertFalse((out / 'memory/stop').exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(launch['pid'], 0)

    def test_c3_monitor_readiness_failure_prevents_probe_launch(self):
        for mode in ('FAKE_PROBE_MONITOR_START_RC', 'FAKE_PROBE_NO_READY'):
            with self.subTest(mode=mode):
                self.env[mode] = '7' if mode.endswith('_RC') else '1'
                run = self.invoke('c3-pagecache-protocol.sh', mode, '0', 'none',
                                  '0', str(self.binary), '--acceptance')
                del self.env[mode]
                self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
                out = self.work / f'c3-{mode}'
                self.assertFalse((out / 'server.pid').exists())
                self.assertFalse((self.work / f'e2e-{mode}').exists())
                self.assertTrue((out / 'monitor-exit.json').is_file())

    def test_c3_warmup_failure_cleans_probe_and_monitor(self):
        self.env['FAKE_WARMUP_RC'] = '5'
        run = self.invoke('c3-pagecache-protocol.sh', 'warmup-fail', '0', 'none',
                          '0', str(self.binary), '--acceptance')
        self.assertNotEqual(run.returncode, 0)
        out = self.work / 'c3-warmup-fail'
        self.assertEqual((out / 'memory-phase.txt').read_text().strip(), 'shutdown')
        observations = json.loads((out / 'memory/observations.json').read_text())
        self.assertTrue(observations['natural_exit'])
        with self.assertRaises(ProcessLookupError):
            os.kill(int((out / 'server.pid').read_text()), 0)
        self.assertFalse((self.work / 'e2e-warmup-fail').exists())

    def test_c3_propagates_monitor_failure_and_records_fallback(self):
        self.env.update(FAKE_PROBE_MONITOR_RC='9', FAKE_MONITOR_WAIT_FOR_STOP='1')
        run = self.invoke('c3-pagecache-protocol.sh', 'monitor-fail', '0', 'none',
                          '0', str(self.binary), '--acceptance')
        self.assertEqual(run.returncode, 9, run.stdout + run.stderr)
        out = self.work / 'c3-monitor-fail'
        status = json.loads((out / 'monitor-exit.json').read_text())
        self.assertEqual(status['monitor_rc'], 9)
        self.assertEqual(status['controller_stop_reason'], 'natural_exit_timeout')
        self.assertFalse((self.work / 'e2e-monitor-fail').exists())

    def test_c3_rejects_probe_exit_failure_after_successful_warmup(self):
        self.env['FAKE_PROBE_EXIT_RC'] = '11'
        run = self.invoke('c3-pagecache-protocol.sh', 'probe-exit-fail', '0', 'none',
                          '0', str(self.binary), '--acceptance')
        self.assertEqual(run.returncode, 11, run.stdout + run.stderr)
        out = self.work / 'c3-probe-exit-fail'
        warmup = json.loads((out / 'warmup.json').read_text())
        self.assertEqual(warmup['finish'], 'length')
        status = json.loads((out / 'server-exit.json').read_text())
        self.assertEqual(status['server_rc'], 11)
        self.assertEqual(status['pid'], int((out / 'server.pid').read_text()))
        self.assertTrue((out / 'monitor-exit.json').is_file())
        self.assertFalse((self.work / 'e2e-probe-exit-fail').exists())

    def final_assets(self):
        assets = self.work / 'assets'
        (assets / 'hot-lists').mkdir(parents=True)
        for name in ('compare-report-r3-c256.txt', 'compare-report-r3-nu15552.txt',
                     'section5-branch.txt'):
            (assets / name).write_text('synthetic prior stage\n')
        (assets / 'verify-c1.log').write_text('synthetic FAIL=0\n')
        cap = {str(i): 256 for i in range(48)}
        hot = {str(i): list(range(256)) for i in range(48)}
        (assets / 'hot-lists/cap-final-12288.json').write_text(json.dumps(cap))
        (assets / 'hot-lists/hot-final-12288.json').write_text(json.dumps(hot))
        self.env.update(Q4T_ACCEPTANCE_ASSETS_DIR=str(assets),
                        BASE_BIN=str(self.binary), CAND_BIN=str(self.binary))
        return assets

    def test_final_propagates_compare_failure_and_preserves_sessions(self):
        self.final_assets()
        self.env.update(FAKE_COMPARE_RC='1', FAKE_MEMORY_RC='0')
        for _ in range(2):
            run = self.invoke('final-acceptance.sh', '12288')
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        sessions = list(self.work.glob('final-12288-*'))
        self.assertEqual(len(sessions), 2)
        for session in sessions:
            status = json.loads((session / 'acceptance-exit.json').read_text())
            self.assertEqual(status['compare_rc'], 1)
            self.assertFalse(status['passed'])

    def test_final_missing_memory_remains_unknown_even_with_overrun_authorized(self):
        self.final_assets()
        self.env['FAKE_COMPARE_RC'] = '0'
        run = self.invoke('final-acceptance.sh', '12288')
        self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
        session = next(self.work.glob('final-12288-*'))
        for stage in ('probe', 'matrix'):
            memory = json.loads((session / f'memory-gate-{stage}.json').read_text())
            self.assertEqual(memory['gate'], 'INDETERMINATE')
            self.assertIsNone(memory['candidate_total'])
            self.assertTrue(memory['user_approved_overrun'])

    def test_final_requires_both_independent_memory_gates(self):
        self.final_assets()
        self.env.update(FAKE_COMPARE_RC='0', FAKE_MEMORY_RC='0')
        for stage in ('probe', 'matrix'):
            with self.subTest(stage=stage):
                self.env[f'FAKE_MEMORY_{stage.upper()}_RC'] = '1'
                run = self.invoke('final-acceptance.sh', '12288')
                del self.env[f'FAKE_MEMORY_{stage.upper()}_RC']
                self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        sessions = list(self.work.glob('final-12288-*'))
        self.assertEqual(len(sessions), 2)
        for session in sessions:
            status = json.loads((session / 'acceptance-exit.json').read_text())
            self.assertFalse(status['passed'])
            self.assertEqual(sorted(s['rc'] for s in status['memory'].values()), [0, 1])
            for stage, prefix in [('probe', 'c3'), ('matrix', 'e2e')]:
                args = json.loads((session / f'memory-gate-{stage}.argv.json').read_text())
                self.assertEqual(args[args.index('--memory-dir') + 1],
                                 str(session / f'{prefix}-acc-final-12288/memory'))

    def test_final_rejects_missing_report_despite_zero_accounting_exit(self):
        self.final_assets()
        self.env.update(FAKE_COMPARE_RC='0', FAKE_MEMORY_RC='0',
                        FAKE_MEMORY_MISSING_STAGE='probe')
        run = self.invoke('final-acceptance.sh', '12288')
        self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
        session = next(self.work.glob('final-12288-*'))
        status = json.loads((session / 'acceptance-exit.json').read_text())
        self.assertEqual(status['memory']['probe']['accounting_rc'], 0)
        self.assertEqual(status['memory']['probe']['rc'], 2)
        self.assertIsNone(status['memory']['probe']['gate'])
        self.assertFalse(status['passed'])

    def test_final_rejects_missing_source_despite_zero_accounting_exit(self):
        self.final_assets()
        self.env.update(FAKE_COMPARE_RC='0', FAKE_MEMORY_RC='0',
                        FAKE_MEMORY_DROP_SOURCE_STAGE='probe')
        run = self.invoke('final-acceptance.sh', '12288')
        self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
        session = next(self.work.glob('final-12288-*'))
        status = json.loads((session / 'acceptance-exit.json').read_text())
        self.assertEqual(status['memory']['probe']['accounting_rc'], 0)
        self.assertEqual(status['memory']['probe']['rc'], 2)
        self.assertFalse(status['memory']['probe']['evidence_present'])
        self.assertFalse(status['passed'])

    def test_final_complete_success_requires_both_memory_reports(self):
        self.final_assets()
        self.env.update(FAKE_COMPARE_RC='0', FAKE_MEMORY_RC='0')
        run = self.invoke('final-acceptance.sh', '12288')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        session = next(self.work.glob('final-12288-*'))
        status = json.loads((session / 'acceptance-exit.json').read_text())
        self.assertTrue(status['passed'])
        self.assertEqual(set(status['memory']), {'probe', 'matrix'})
        for stage in status['memory'].values():
            self.assertEqual(stage['rc'], 0)
            self.assertEqual(stage['gate'], 'PASS')
            self.assertTrue(stage['evidence_present'])

    def test_final_rejects_failed_prerequisite(self):
        assets = self.final_assets()
        (assets / 'verify-c1.log').write_text('synthetic FAIL=1\n')
        run = self.invoke('final-acceptance.sh', '12288')
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(list(self.work.glob('final-12288-*')), [])


if __name__ == '__main__':
    unittest.main()
