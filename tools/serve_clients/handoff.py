"""Targeted terminal-handoff repair; preserve the original failed short run."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_clients import cpu, short as s

BASE = cpu.BASE
BUILD = BASE/'handoff-build'
ENGINE = BASE/'engine-handoff'
HOST = BASE/'host-handoff'
PLAN = BASE/'handoff-plan.json'
OUT = BASE/'handoff-short'
LIMITS = dict(starts=1, generation_attempts=15, controls=100, seconds=600)
CHANGED = {'CMakeLists.txt', 'include/q4t/server/client_policy.h',
           'include/q4t/server/http_response.h', 'src/server/client_policy.cpp',
           'src/server/chat_http.cpp', 'tests/server_client_policy_test.cpp',
           'tools/release/required_host_tests.txt', 'tools/serve_audit/inventory.json'}


def build():
    BUILD.mkdir()
    cpu.save(BUILD/'sources.json', cpu.sources())
    cpu.ENGINE, cpu.HOST = ENGINE, HOST
    # The unchanged SSD binary is reused; no previous candidate is overwritten.
    return cpu.build(BUILD)


def host():
    built = s.pkg.read(BUILD/'result.json')
    s.require(built['passed'], 'repair build failed')
    s.verify_sources(s.pkg.read(BUILD/'sources.json'))
    for name, digest in built['binaries'].items():
        s.require(cpu.sha(ROOT/name) == digest, 'repair binary changed')
    out = BASE/'handoff-cpu'
    out.mkdir()
    listed = subprocess.check_output([str(HOST/'q4t_host_tests'), '--list'], text=True)
    selected = [name for name in listed.splitlines() if
                name.startswith(('client_', 'server_http_'))]
    s.require(len(selected) >= 15, 'targeted host inventory empty')
    (out/'required.txt').write_text('\n'.join(selected)+'\n')
    checks = {
        'targeted-off': cpu.command(out, 'targeted-off', [str(HOST/'q4t_host_tests'),
            '--required-list', str(out/'required.txt')]),
        'targeted-on': cpu.command(out, 'targeted-on', [str(ENGINE/'q4t_host_tests'),
            '--required-list', str(out/'required.txt')]),
        'inventory': cpu.command(out, 'inventory', ['/usr/bin/python3', '-B',
            'tools/serve_audit/check_inventory.py', '--self-check'])}
    cpu.save(out/'result.json', dict(passed=all(checks.values()), tests_per_mode=len(selected),
        checks=checks, sources=s.pkg.read(BUILD/'sources.json'),
        reused_cpu=str(BASE/'cpu-v1/result.json'), http_listeners=0, gpu_execution=0))
    s.require(all(checks.values()), 'targeted host failed')


def prepare():
    old = s.pkg.read(s.PLAN)
    failed = s.pkg.read(BASE/'short/result.json')
    s.require(failed['status'] == 'FAILED' and 'project-read' in failed['error'], 'original failure missing')
    s.require(s.pkg.read(BASE/'short/lease/exit.json')['status'] == 'COMMAND_FAILED', 'original guard result')
    s.require(not PLAN.exists() and not OUT.exists(), 'preserve corrective run')
    s.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT), 'commit correction first')
    tested = s.pkg.read(BASE/'handoff-cpu/result.json')
    s.require(tested['passed'], 'corrective CPU missing')
    s.verify_sources(tested['sources'])
    changed = {name for name, digest in old['sources'].items() if cpu.sha(ROOT/name) != digest}
    s.require(changed <= CHANGED and changed, 'unexpected repair scope: '+str(changed))
    built = s.pkg.read(BUILD/'result.json')
    protocol = s.pkg.read(ENGINE/'generated/q4t/server/chat_protocol_sources.json')
    digest = hashlib.sha256(''.join(f'{n}:{protocol[n]}\n' for n in sorted(protocol)).encode()).hexdigest()
    plan = copy.deepcopy(old)
    plan.update(limits=LIMITS, binary=str(ENGINE/'q4t'), lifecycle=str(ENGINE/'q4t_http_lifecycle_tests'),
        binaries=built['binaries'], protocol=digest,
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip())
    paths = set(old['sources']) | set(tested['sources']) | {
        'tools/serve_clients/handoff.py', 'docs/SERVE_CLIENT_HANDOFF_2026-11-04.md'}
    plan['sources'] = {name: cpu.sha(ROOT/name) for name in sorted(paths)}
    plan['local'].update({str(p): cpu.sha(p) for p in [s.PLAN, BASE/'short/result.json',
        BASE/'short/lease/exit.json', BASE/'handoff-cpu/result.json', BUILD/'result.json']})
    plan['reuse'] = dict(original_failed=True, sources_changed=sorted(changed),
        original_native_http_passes=27, original_model_starts=2,
        original_successful_generations=4, original_cancelled_generations=1,
        original_tool_executions=0, original_cache_generations=0,
        reused='legacy quality; authentication/header identity, input/output policy negatives and preflight; namespace cancellation result',
        repeated='2 affected native HTTP/client admission contracts and scoped cancellation/quota; strict JSON immediately followed by tool',
        remaining='real tool call/result/citation and private/shared model cache isolation',
        performance_acceptance=False)
    cpu.save(PLAN, plan)
    return dict(prepared=True, limits=LIMITS, models_started=0)


class Run(s.Run):
    def execute(self):
        OUT.mkdir(mode=0o700, exist_ok=True)
        lock = s.a.guard.common_lock()
        info = lock.stat()
        s.require((info.st_dev, info.st_ino) == (66305, 3294067), 'original lock changed')
        lease = s.pkg.read(lock)
        s.require(lease['pid'] == os.getppid() and lease['state'] == 'held' and
                  not s.a.lock_available(lock), 'requires full-phase guard')
        result = dict(status='FAILED', limits=LIMITS, installed=False, long_run=False)
        def interrupted(*_):
            raise RuntimeError('corrective short interrupted or expired')
        signals = {v: signal.signal(v, interrupted) for v in
                   (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)}
        signal.alarm(LIMITS['seconds'])
        try:
            s.a.require_idle(OUT/'initial-gpu.json')
            native = self.children.launch(OUT/'lifecycle', [self.plan['lifecycle'], 'http_client_'])
            self.children.wait(native, OUT/'lifecycle', 30)
            s.require('2 tests, 2 passed, 0 failed, 0 skipped' in
                      (OUT/'lifecycle/stdout.log').read_text(), 'targeted HTTP failure')
            self.launch(True)
            self.cancellation()
            value = self.complete('scoped-structured', s.a.strict_json())
            s.require(json.loads(value.message['content']) == {'ok': True}, 'strict quality')
            # No idle, sleep, retry or larger quota between terminal and tool.
            self.tools('project')
            self.caches()
            value = self.complete('scoped-quality', s.a.ordinary(True))
            s.require(value.message['content'].strip() == '710003', 'recovery quality')
            cpu.save(OUT/'final-metrics.json', self.idle('scoped-idle'))
            self.stop(True)
            for folder in ('scoped-model', 'nginx'):
                for name in ('stdout.log', 'stderr.log'):
                    raw = (OUT/folder/name).read_bytes()
                    for secret in [self.policy['gateway_token'],
                                   *(c['api_key'] for c in self.front['clients'])]:
                        s.require(secret.encode() not in raw, 'credential leaked to log')
            self.children.reap(OUT)
            s.a.require_idle(OUT/'final-gpu.json')
            s.prepared_identity(self.plan)
            s.require(self.successes == 10 and self.cancelled == 1, 'corrective accounting')
            result.update(status='PASS_BOUNDED_CLIENT_HANDOFF', native_tests=2,
                          successful_generations=10, cancelled_generations=1,
                          real_tools=1, cleanup_confirmed=True)
        except BaseException as error:
            result['error'] = repr(error)
            raise
        finally:
            signal.alarm(0)
            for connection in self.sockets:
                connection.close()
            self.children.cleanup()
            for v, previous in signals.items():
                signal.signal(v, previous)
            result.update(counts=self.counts, seconds=time.monotonic()-self.started)
            cpu.save(OUT/'result.json', result)
        return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'host', 'prepare', 'check', 'run'))
    args = parser.parse_args()
    if args.action in ('build', 'host', 'prepare'):
        value = globals()[args.action]()
    else:
        s.OUT, s.LIMITS = OUT, LIMITS
        plan = s.prepared_identity(s.pkg.read(PLAN))
        if args.action == 'check':
            value = dict(checked=True, models_started=0)
        else:
            s.require(not (OUT/'result.json').exists(), 'preserve correction evidence')
            with s.sup.processes.child_subreaper():
                value = Run(plan).execute()
    print(json.dumps(value))


if __name__ == '__main__':
    main()
