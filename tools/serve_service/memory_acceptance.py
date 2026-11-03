"""Real managed memory stop at a high floor, preserving host safety reserves."""
import json
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_service import acceptance as a, package as pkg
from tools.serve_service import supervisor
from tools.serve_pressure import pressure

BASE = ROOT/'.q4t-work/serve-memory-stop-20261103'
PLAN, OUT = BASE/'plan.json', BASE/'execution'
LIMITS = dict(starts=1, generations=4, negatives=1, observations=240, seconds=600)
FLOOR_MIB = 30720
SOURCES = (*a.SOURCES, 'tools/serve_service/memory_acceptance.py',
    'tools/serve_service/test_memory.py', 'tools/serve_service/test_service.py',
    'tools/serve_service/test_memory_acceptance.py',
    'tools/serve_pressure/pressure.py', 'docs/SERVE_MEMORY_STOP_2026-11-03.md')


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists(), 'preserve prior attempt')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
                'commit reviewed implementation and driver first')
    cpu = pkg.read(BASE/'cpu-final/result.json')
    pkg.require(cpu['passed'], 'CPU checks failed')
    for path, digest in cpu['source_sha256'].items():
        pkg.require(pkg.sha(ROOT/path) == digest, 'CPU source changed: '+path)
    release = pkg.read(ROOT/'build/q4t.release.json')
    for name, digest in pkg.read(release['tested_source_manifest']).items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'runtime source changed: '+name)
    receipt = pkg.prepare(ROOT/'build/q4t', ROOT/'build/q4t.release.json',
        ROOT.parent/'llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream',
        BASE/'package', port=18121, restart_limit=3,
        critical_memory_mib=FLOOR_MIB)
    token = secrets.token_hex(10)
    pkg.save(PLAN, dict(version=1, limits=LIMITS, gpu_window_seconds=a.GPU_WINDOW,
        package=receipt, token=token, unit='q4t-memory-stop-'+token+'.service',
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                              text=True).strip(),
        source_files={p: pkg.sha(ROOT/p) for p in SOURCES},
        cpu_result_sha256=pkg.sha(BASE/'cpu-final/result.json'),
        binary_sha256=release['binary_sha256'],
        default_release_sha256=pkg.sha(ROOT/'build/q4t.release.json'),
        pressure=dict(max_bytes=pressure.MAX_BYTES,
            min_available_bytes=pressure.MIN_AVAILABLE,
            min_free_bytes=pressure.MIN_FREE, critical_memory_mib=FLOOR_MIB),
        planned=dict(starts=1, generations=4, negative_scenarios=1),
        supervisor_http_upper_bound=188, explicit_control_limit=a.CONTROL_LIMIT,
        physical_ram_hard_limit=False, performance_acceptance=False))


def check():
    plan = pkg.read(PLAN)
    pkg.require(plan['limits'] == LIMITS and
                plan['gpu_window_seconds'] == a.GPU_WINDOW, 'budget changed')
    for path, digest in plan['source_files'].items():
        pkg.require(pkg.sha(ROOT/path) == digest, 'frozen source changed: '+path)
    pkg.require(pkg.sha(BASE/'cpu-final/result.json') == plan['cpu_result_sha256'],
                'CPU receipt changed')
    pkg.verify(plan['package']['package'], plan['package']['manifest_sha256'])
    pkg.require(pkg.sha(ROOT/'build/q4t') == plan['binary_sha256'] and
        pkg.sha(ROOT/'build/q4t.release.json') == plan['default_release_sha256'],
        'default pair changed')
    return plan


def protected_terminal(value):
    worker, guard = value['worker'], value['guard']
    pkg.require(value['state'] == worker['state'] == 'FAILED' and
        worker['cleanup_confirmed'] and value['cleanup_confirmed'] and
        not value['automatic_reactivation'] and
        not value['manual_stop'] and worker['attempts'] == 1 and
        worker['restarts'] == 0 and worker['error'] == 'critical_memory_pressure'
        and worker['memory']['state'] == 'TRIPPED' and
        worker['memory']['reason'] == 'critical_memory_pressure' and
        worker['memory']['signal_sent'] and
        worker['memory']['available_bytes'] < worker['memory']['floor_bytes'] and
        guard['status'] == 'COMMAND_FAILED' and guard['child_returncode'] == 1 and
        'cleanup_error' not in guard, 'protective terminal differs')


class Run(a.Run):
    def trip(self):
        self.budget('negatives')
        pkg.save(OUT/'pressure-before.json', pressure.memory())
        floor = FLOOR_MIB*pressure.MIB
        with_pressure = pressure.Pressure(OUT)
        try:
            row = with_pressure.command('status')
            pkg.require(row['after']['MemAvailable'] >= floor,
                        'pressure scenario started below critical floor')
            for _ in range(pressure.MAX_BYTES//pressure.CHUNK):
                self.budget()
                row = with_pressure.command('add')
                if row['after']['MemAvailable'] < floor-pressure.CHUNK:
                    break
            pkg.require(row['after']['MemAvailable'] < floor-pressure.CHUNK,
                        'bounded pressure did not cross the fixed floor')
            pkg.save(OUT/'pressure-trigger.json', row)
            until = time.monotonic()+45
            while not (self.run_dir/'terminal.json').exists():
                self.budget()
                pkg.require(time.monotonic() < until, 'protective stop timeout')
                # Keep helper alive and archive fresh real host observations.
                with_pressure.command('status')
                time.sleep(.2)
        finally:
            with_pressure.close()
        signal.setitimer(signal.ITIMER_REAL, 0)
        self.gpu_finished = time.monotonic()
        pkg.require(self.gpu_finished-self.gpu_started <= a.GPU_WINDOW,
                    'GPU window exceeded')
        terminal = pkg.read(self.run_dir/'terminal.json')
        protected_terminal(terminal)
        pkg.save(OUT/'protective-stop-verified.json', dict(passed=True,
            terminal_sha256=pkg.sha(self.run_dir/'terminal.json'),
            memory=terminal['worker']['memory']))
        again = self.service_cli('idempotent-stop', 'stop')
        pkg.require(json.loads(again.stdout) == terminal,
                    'stop hid protective failure or changed terminal')
        time.sleep(10)
        props = self.owned_properties('unit-after-pressure')
        pkg.require(props['ActiveState'] == 'failed' and props['MainPID'] == '0'
            and props['NRestarts'] == '0' and props['ExecMainStatus'] == '1',
            'protective unit exit or no-restart mismatch')
        pkg.require(len(list(self.run_dir.glob('attempt-*.json'))) == 1,
                    'memory trip reactivated backend')
        for owner in self.owners:
            try:
                pkg.require(supervisor.process_identity(owner['pid']) != owner,
                            'owned process remains')
            except (FileNotFoundError, ProcessLookupError):
                pass
        with socket.socket() as client:
            pkg.require(client.connect_ex(('127.0.0.1', self.port)) != 0,
                        'listener remains after protective stop')
        a.require_idle(OUT/'gpu-after-pressure.json')
        pkg.require(a.lock_available(a.guard.common_lock()), 'lease not released')

    def execute(self):
        OUT.mkdir(mode=0o700)
        pkg.save(OUT/'begin.json', dict(plan=self.plan, unix_seconds=time.time()))
        result = dict(status='FAILED', physical_ram_hard_limit=False,
            performance_acceptance=False, persistent_service_installed=False)
        def expired(*_):
            raise RuntimeError('bounded GPU window expired')
        previous = signal.signal(signal.SIGALRM, expired)
        try:
            a.require_idle(OUT/'gpu-before.json')
            self.launch()
            self.plain('ordinary-json')
            structured = self.complete('strict-json', a.strict_json())
            pkg.require(json.loads(structured.message['content']) == {'ok': True}
                and structured.finish_reason == 'stop', 'strict JSON failed')
            self.tools('project')
            self.drain('idle-before-pressure', 4)
            state = self.wait_state('READY', 1)
            pkg.require(state['memory']['state'] == 'OPEN', 'memory monitor not open')
            self.trip()
            result.update(status='PASS_MANAGED_MEMORY_STOP', starts=1,
                successful_requests=4, real_tool_executions=1,
                automatic_restarts=0, terminal_guard_status='COMMAND_FAILED',
                protective_failure_preserved=True)
        except BaseException as error:
            result['error'] = repr(error)
            raise
        finally:
            try:
                self.cleanup()
                a.require_idle(OUT/'gpu-final.json')
                pkg.require(a.lock_available(a.guard.common_lock()), 'shared lease busy')
                result['cleanup_confirmed'] = True
                pkg.require(time.monotonic()-self.started < LIMITS['seconds'],
                            'total phase deadline')
            except BaseException as error:
                result.update(status='FAILED', cleanup_error=repr(error))
                raise
            finally:
                signal.signal(signal.SIGALRM, previous)
                result.update(counts=self.counts,
                    elapsed_seconds=time.monotonic()-self.started,
                    gpu_window_elapsed=None if self.gpu_finished is None else
                        self.gpu_finished-self.gpu_started)
                pkg.save(OUT/'result.json', result)


if __name__ == '__main__':
    a.BASE, a.PLAN, a.OUT, a.LIMITS = BASE, PLAN, OUT, LIMITS
    if sys.argv[1:] == ['prepare']:
        prepare()
    elif sys.argv[1:] == ['execute']:
        Run(check()).execute()
    else:
        raise SystemExit('prepare | execute')
