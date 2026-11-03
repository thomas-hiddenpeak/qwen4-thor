"""Finish real managed lifecycle acceptance on the accepted handoff repair."""
import json
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_service import acceptance as a, package as pkg, supervisor

FIRST = a.BASE
BASE = FIRST/'supplement'
PLAN, OUT = BASE/'plan.json', BASE/'execution'
LIMITS = dict(starts=3, generations=6, negatives=5, observations=240, seconds=1200)
SOURCES = (*a.SOURCES, 'tools/serve_service/supplement.py',
           'docs/SERVE_MANAGED_SERVICE_SUPPLEMENT_2026-11-03.md')


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists(), 'preserve previous supplement')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
                'commit reviewed supplement first')
    original_cpu = pkg.read(FIRST/'cpu/result.json')
    for name, digest in original_cpu['source_sha256'].items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'managed CPU implementation changed')
    original_plan = pkg.read(FIRST/'gpu-plan.json')
    original_result = pkg.read(FIRST/'execution/result.json')
    pkg.require(original_result['status'] == 'FAILED' and original_result['cleanup_confirmed']
                and original_result['counts']['negatives'] == 3, 'different first attempt')
    reused = {}
    for label, code in (('lease-contention', 75), ('duplicate-runtime', 1), ('changed-manifest', 1)):
        path = FIRST/'execution'/(label+'.json')
        pkg.require(pkg.read(path)['returncode'] == code, 'missing original negative')
        reused[str(path)] = pkg.sha(path)
    pkg.require(pkg.read(FIRST/'execution/rejected-lease/exit.json')['status'] == 'LEASE_BUSY',
                'lease contention was not rejected before command')
    release = pkg.read(ROOT/'build/q4t.release.json')
    pkg.require(release['acceptance'] == 'ACCEPT_BOUNDED_STRUCTURED_HANDOFF', 'handoff not accepted')
    for name, digest in pkg.read(release['tested_source_manifest']).items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'runtime source changed')
    BASE.mkdir()
    receipt = pkg.prepare(ROOT/'build/q4t', ROOT/'build/q4t.release.json',
        pkg.read(FIRST/'package-cpu/manifest.json')['model_dir'], BASE/'package',
        port=18120, restart_limit=2)
    token = secrets.token_hex(10)
    pkg.save(PLAN, dict(version=1, limits=LIMITS, gpu_window_seconds=a.GPU_WINDOW,
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        source_files={p:pkg.sha(ROOT/p) for p in SOURCES}, package=receipt,
        default_release_sha256=pkg.sha(ROOT/'build/q4t.release.json'),
        binary_sha256=release['binary_sha256'], protocol_sha256=release['chat_protocol']['protocol_sha256'],
        unit='q4t-managed-srv14-fix-'+token+'.service', token=token,
        planned=dict(starts=2, generations=6, negative_scenarios=3),
        reused_negatives=reused, reused_cpu_sha256=pkg.sha(FIRST/'cpu/result.json'),
        original_plan_sha256=pkg.sha(FIRST/'gpu-plan.json'),
        original_failure_sha256=pkg.sha(FIRST/'execution/result.json'),
        original_runtime_differs=original_plan['binary_sha256'] != release['binary_sha256'],
        supervisor_http_reservation=188, explicit_control_limit=a.CONTROL_LIMIT,
        performance_acceptance=False, operator_notifications=False))


def check():
    plan = pkg.read(PLAN)
    pkg.require(plan['limits'] == LIMITS and plan['gpu_window_seconds'] == a.GPU_WINDOW, 'budget drift')
    for path, digest in plan['source_files'].items():
        pkg.require(pkg.sha(ROOT/path) == digest, 'source drift: '+path)
    for path, digest in plan['reused_negatives'].items():
        pkg.require(pkg.sha(path) == digest, 'reused evidence changed')
    pkg.require(pkg.sha(FIRST/'cpu/result.json') == plan['reused_cpu_sha256'] and
        pkg.sha(FIRST/'execution/result.json') == plan['original_failure_sha256'], 'original receipt changed')
    pkg.verify(plan['package']['package'], plan['package']['manifest_sha256'])
    pkg.require(pkg.sha(ROOT/'build/q4t') == plan['binary_sha256'] and
        pkg.sha(ROOT/'build/q4t.release.json') == plan['default_release_sha256'], 'installed pair changed')
    return plan


class Run(a.Run):
    def execute(self):
        OUT.mkdir(mode=0o700)
        pkg.save(OUT/'begin.json', dict(monotonic=self.started, unix_seconds=time.time(), plan=self.plan))
        result = dict(status='FAILED', automatic_request_retry=False,
                      performance_acceptance=False, persistent_service_installed=False)
        def expired(*_):
            raise RuntimeError('bounded supervision/control window expired')
        previous = signal.signal(signal.SIGALRM, expired)
        try:
            a.require_idle(OUT/'gpu-before.json')
            self.launch()
            structured = self.complete('strict-json', a.strict_json())
            pkg.require(json.loads(structured.message['content']) == {'ok': True} and
                        structured.finish_reason == 'stop', 'strict JSON failure')
            self.tools('first-project')
            self.drain('first-idle', 3)
            first = self.wait_state('READY', 1)
            self.kill_backend(first, 'first')
            second = self.wait_state('READY', 2)
            pkg.require(not supervisor.processes.group_members(first['backend']['pid']),
                        'old group survived replacement')
            self.bind(second, 'second-ready')
            self.plain('recovered-json')
            self.tools('second-project')
            self.drain('second-idle', 3)
            self.kill_backend(second, 'second')
            waiting = self.wait_state('RESTART_WAIT', 2, timeout=20)
            pkg.require(waiting['restarts'] == 2, 'wrong pending restart count')
            pkg.save(OUT/'stop-during-backoff.json', waiting)
            self.stop()
            pkg.require(self.counts['generations'] == 6 and self.counts['negatives'] == 3,
                        'frozen supplement counts differ')
            result.update(status='PASS_MANAGED_SYSTEMD_MTP', starts=2,
                successful_requests=6, real_tool_executions=2, actual_backend_replacements=1,
                pending_restarts_cancelled=1, reused_negative_scenarios=3)
        except BaseException as error:
            result['error'] = repr(error)
            raise
        finally:
            try:
                self.cleanup()
                a.require_idle(OUT/'gpu-final.json')
                pkg.require(a.lock_available(a.guard.common_lock()), 'shared lease not released')
                result['cleanup_confirmed'] = True
                pkg.require(time.monotonic()-self.started < LIMITS['seconds'], 'total phase deadline')
            except BaseException as error:
                result.update(status='FAILED', cleanup_error=repr(error))
                raise
            finally:
                signal.signal(signal.SIGALRM, previous)
                result.update(counts=self.counts, elapsed_seconds=time.monotonic()-self.started,
                    supervision_http_upper_bound=188 if result['status'].startswith('PASS') else None,
                    full_observation_upper_bound=188+self.counts['controls']
                        if result['status'].startswith('PASS') else None,
                    gpu_window_elapsed=None if self.gpu_finished is None else
                        self.gpu_finished-self.gpu_started)
                pkg.save(OUT/'result.json', result)


if __name__ == '__main__':
    a.BASE, a.PLAN, a.OUT, a.LIMITS = BASE, PLAN, OUT, LIMITS
    if sys.argv[1:] == ['prepare']: prepare()
    elif sys.argv[1:] == ['execute']: Run(check()).execute()
    else: raise SystemExit('prepare | execute')
