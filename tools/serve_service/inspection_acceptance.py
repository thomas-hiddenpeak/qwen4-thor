"""One bounded real generation for the read-only managed inspection command."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_service import acceptance as a, package as pkg, supervisor as sup

BASE = ROOT/'.q4t-work/serve-inspection-20261103'
PLAN, OUT = BASE/'plan.json', BASE/'execution'
LEGACY = ROOT/'.q4t-work/serve-controller-death-20261103/execution/managed-run'
LIMITS = dict(starts=1, generations=3, negatives=0, observations=220, seconds=600)
SOURCES = sorted(set((*a.SOURCES, *pkg.SOURCES,
    'tools/serve_service/inspection_acceptance.py',
    'tools/serve_service/test_inspection.py', 'tools/serve_service/test_service.py',
    'tools/serve_service/controller_exec.py', 'docs/SERVE_INSPECTION_2026-11-03.md')))


def configure():
    a.BASE, a.PLAN, a.OUT = BASE, PLAN, OUT
    a.LIMITS, a.CONTROL_LIMIT, a.GPU_WINDOW = LIMITS, 32, 220


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists(), 'preserve previous phase')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
                'commit reviewed sources before freeze')
    cpu = pkg.read(BASE/'cpu/result.json')
    pkg.require(cpu['passed'] and cpu['tests_run'] == 51 and
                cpu['isolated_entry_passed'], 'CPU acceptance incomplete')
    for name, digest in cpu['sources'].items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'CPU source changed: '+name)
    old = pkg.read(LEGACY/'controller.json')
    manifest = pkg.verify(old['package'], old['manifest_sha256'])
    release = pkg.read(ROOT/'build/q4t.release.json')
    for name, digest in pkg.read(release['tested_source_manifest']).items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'C++ runtime changed: '+name)
    receipt = pkg.prepare(ROOT/'build/q4t', ROOT/'build/q4t.release.json',
        manifest['model_dir'], BASE/'package', port=18123, restart_limit=0)
    pkg.save(PLAN, dict(version=1, limits=LIMITS, package=receipt,
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                             cwd=ROOT, text=True).strip(),
        source_files={name:pkg.sha(ROOT/name) for name in SOURCES},
        cpu_result_sha256=pkg.sha(BASE/'cpu/result.json'),
        default_release_sha256=pkg.sha(ROOT/'build/q4t.release.json'),
        unit='unused-no-systemd-unit.service', token='inspection-only',
        gpu_window_seconds=220, explicit_control_limit=32,
        supervisor_http_upper_bound=188, performance_acceptance=False))
    return dict(plan=str(PLAN), package=receipt)


def check():
    plan = pkg.read(PLAN)
    pkg.require(plan['limits'] == LIMITS and plan['gpu_window_seconds'] == 220 and
                plan['explicit_control_limit'] == 32, 'budget changed')
    for name, digest in plan['source_files'].items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'phase source changed: '+name)
    pkg.require(pkg.sha(BASE/'cpu/result.json') == plan['cpu_result_sha256'],
                'CPU receipt changed')
    pkg.verify(plan['package']['package'], plan['package']['manifest_sha256'])
    pkg.require(pkg.sha(ROOT/'build/q4t') == plan['package']['binary_sha256'] and
        pkg.sha(ROOT/'build/q4t.release.json') == plan['default_release_sha256'],
        'default pair changed')
    return plan


def record_hashes(directory):
    return {str(p.relative_to(directory)):pkg.sha(p) for p in directory.rglob('*')
            if p.is_file()}


class Run(a.Run):
    def __init__(self, plan):
        super().__init__(plan)
        self.process, self.log = None, None

    def launch(self):
        self.log = (OUT/'controller.log').open('xb')
        command = [self.manifest['python']['path'], '-I', '-B',
            ROOT/'tools/serve_service/controller_exec.py', '--plan', PLAN,
            '--output', self.run_dir, '--parent', str(os.getpid())]
        self.gpu_started = time.monotonic()
        signal.setitimer(signal.ITIMER_REAL, 220)
        self.process = subprocess.Popen(list(map(str, command)), cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)
        state = self.wait_state('READY', 1)
        self.controller = pkg.read(self.run_dir/'controller.json')
        pkg.require(self.controller['owner'] == sup.process_identity(self.process.pid),
                    'controller identity mismatch')
        self.owners = [self.controller['owner'], state['owner'], state['backend']]
        pkg.require(not a.lock_available(a.guard.common_lock()), 'lease not held')
        pkg.save(OUT/'ready-owners.json', self.owners)

    def inspect_cli(self, label, directory, expected):
        return json.loads(self.command(label, [self.manifest['python']['path'],
            '-I', '-B', self.package/'source/tools/serve_service/service.py',
            'inspect', '--run', directory], expected=expected).stdout)

    def stop_owned(self):
        if self.process is None or self.process.poll() is not None:
            return
        if (self.run_dir/'controller.json').exists():
            self.service_cli('explicit-stop', 'stop')
        else:
            # Only the direct child whose identity was captured by Popen.
            self.process.terminate()
        pkg.require(self.process.wait(50) == 0, 'controller did not stop normally')
        self.gpu_finished = time.monotonic()
        signal.setitimer(signal.ITIMER_REAL, 0)
        pkg.require(self.gpu_finished-self.gpu_started <= 220, 'GPU window exceeded')

    def execute(self):
        OUT.mkdir(mode=0o700)
        pkg.save(OUT/'begin.json', dict(monotonic=self.started, unix_seconds=time.time()))
        result = dict(status='FAILED', starts=0, successful_requests=0,
            performance_acceptance=False, persistent_service_installed=False)
        def expired(*_):
            raise RuntimeError('bounded GPU window expired')
        previous = signal.signal(signal.SIGALRM, expired)
        try:
            a.require_idle(OUT/'gpu-before.json')
            before = record_hashes(LEGACY)
            legacy = self.inspect_cli('legacy-inspection', LEGACY, 2)
            pkg.require(not legacy['complete'] and not legacy['cleanup_observed'] and
                legacy['errors'] == ['legacy_or_different_observation_scope'] and
                legacy['lock']['state'] == 'AVAILABLE' and
                legacy['gpu']['status'] == 'OK' and not legacy['gpu']['pids'],
                'legacy uncertainty or current resource observation lost')
            pkg.require(record_hashes(LEGACY) == before, 'legacy records mutated')
            pkg.save(OUT/'legacy-preserved.json', before)
            self.launch()
            result['starts'] = 1
            # The live guard may create/reap an NVML observer between samples.
            # Preserve that honest uncertainty; never require resampling green.
            live = self.inspect_cli('live-inspection', self.run_dir, None)
            pkg.require(live['run_state'] == 'OWNER_PRESENT' and
                live['resource_availability'] == 'BUSY' and
                live['lock']['state'] == 'BUSY' and live['listeners'] and
                self.owners[-1]['pid'] in live['gpu']['pids'] and
                not live['cleanup_observed'], 'live observation inconsistent')
            pkg.require(set(live['errors']) <= {
                'resources_changed_during_observation',
                'run_records_changed_during_observation'},
                'unexpected live observation failure')
            self.plain('ordinary-json')
            self.tools('project')
            self.drain('idle', 3)
            result['successful_requests'] = 3
            self.stop_owned()
            terminal = pkg.read(self.run_dir/'terminal.json')
            pkg.require(terminal['state'] == 'STOPPED' and terminal['cleanup_confirmed']
                and terminal['guard']['status'] == 'COMPLETED' and
                terminal['worker']['attempts'] == 1 and terminal['worker']['restarts'] == 0,
                'normal stop did not finish')
            time.sleep(10)
            before = record_hashes(self.run_dir)
            lock_before = pkg.sha(a.guard.common_lock())
            stopped = self.inspect_cli('stopped-inspection', self.run_dir, 0)
            pkg.require(stopped['cleanup_observed'] and
                stopped['run_state'] == 'INACTIVE_OBSERVED' and
                stopped['resource_availability'] == 'AVAILABLE',
                'stopped observation incomplete')
            pkg.require(record_hashes(self.run_dir) == before and
                pkg.sha(a.guard.common_lock()) == lock_before, 'inspection mutated records')
            pkg.save(OUT/'stopped-preserved.json', dict(records=before, lock_sha256=lock_before))
            pkg.require(len(list(self.run_dir.glob('attempt-*.json'))) == 1,
                        'unexpected reactivation')
            result.update(status='PASS_READONLY_SERVICE_INSPECTION', real_tool_executions=1,
                          legacy_preserved=True, stopped_records_preserved=True,
                          live_inspection_complete=live['complete'],
                          live_inspection_errors=live['errors'])
        except BaseException as error:
            result['error'] = repr(error)
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            self.cleaning = True
            try:
                self.stop_owned()
                a.require_idle(OUT/'gpu-final.json')
                pkg.require(a.lock_available(a.guard.common_lock()), 'GPU lease retained')
                for owner in self.owners:
                    try:
                        pkg.require(sup.process_identity(owner['pid']) != owner,
                                    'owned process still present')
                    except (FileNotFoundError, ProcessLookupError):
                        pass
                result['cleanup_confirmed'] = True
                pkg.require(time.monotonic()-self.started < 600 and
                            self.counts['controls'] <= 32, 'phase budget exceeded')
            except BaseException as error:
                result.update(status='FAILED', cleanup_error=repr(error))
                raise
            finally:
                signal.signal(signal.SIGALRM, previous)
                if self.log is not None:
                    self.log.close()
                result.update(counts=self.counts, elapsed_seconds=time.monotonic()-self.started,
                    gpu_window_elapsed=None if self.gpu_finished is None else
                        self.gpu_finished-self.gpu_started,
                    supervisor_http_upper_bound=188,
                    full_observation_upper_bound=188+self.counts['controls'])
                pkg.save(OUT/'result.json', result)
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'check', 'run'))
    args = parser.parse_args()
    configure()
    value = prepare() if args.action == 'prepare' else check()
    if args.action == 'run':
        with sup.processes.child_subreaper():
            value = Run(value).execute()
    print(json.dumps(value))
