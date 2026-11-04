"""One lease: TLS deadline peers, then current-model saved-result recovery."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import deadline_cpu as cpu, deadline_cpu_repair as repair
from tools.project_tasks import deadline_probes, engine, store, transport, migration_fixtures as history
from tools.project_tasks import protocol_stage as prior
from tools.serve_service import package as pkg

BASE = cpu.BASE
PLAN, OUT = BASE/'plan.json', BASE/'execution'
ATTEMPT = BASE
HISTORICAL = BASE/'historical-runtime'
TASKS = HISTORICAL/'.q4t-work/tasks'
PORT, TLS_PORT = 18135, 18455
LIMITS = dict(starts=1, generation_attempts=4, controls=48, seconds=240)
GPU_WINDOW = 210
HERE = 'tools/project_tasks/'


def sources():
    names = subprocess.check_output(['git', 'ls-files', 'tools', 'cmake'], cwd=ROOT, text=True).splitlines()
    names += ['docs/MODEL.md', 'docs/serve-config-v1.schema.json',
              'docs/SERVE_TASK_DEADLINE_2026-11-04.md']
    return {name:pkg.sha(ROOT/name) for name in sorted(set(names))}


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists() and not HISTORICAL.exists(), 'preserve execution')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT), 'commit stage first')
    repair.prior()
    receipt = pkg.read(BASE/'cpu-repair/result.json')
    pkg.require(receipt['passed'] and receipt['effective_tests'] == 16 and receipt['tests_run'] == 2,
                'corrected CPU gate')
    for name, digest in receipt['sources'].items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'CPU source changed: '+name)
    driver = pkg.read(BASE/'cpu-driver-final/result.json')
    pkg.require(driver['passed'] and driver['tests_run'] == 2, 'driver CPU gate')
    for name, digest in driver['sources'].items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'driver source changed: '+name)
    historical = history.extract_runtime(8, HISTORICAL)
    private = HISTORICAL/'.q4t-work/private'
    private.mkdir(mode=0o700, parents=True)
    cert, key = prior.s.certificate(private)
    front = prior.s.example(TLS_PORT, PORT)
    front.update(per_client_inflight=4, global_inflight=8, per_client_rps=100, burst=128)
    policy = dict(version=1, gateway_token=prior.s.secrets.token_hex(32), clients=[
        dict(id='alpha', max_inflight=1, max_control_inflight=2,
             max_prompt_tokens=12000, max_output_tokens=512, max_body_bytes=131072),
        dict(id='beta', max_inflight=1, max_control_inflight=2,
             max_prompt_tokens=9000, max_output_tokens=64, max_body_bytes=65536)])
    pkg.save(private/'gateway.json', front)
    pkg.save(private/'clients.json', policy)
    gateway = prior.s.g.prepare(private/'gateway.json', pkg.read(prior.s.VENDOR)['binary'],
        cert, key, private/'gateway', private/'clients.json')
    release = pkg.read(ROOT/'build/q4t.release.json')
    protocol = release['chat_protocol']
    pkg.verify_declarations(release, required=True)
    profiles, bindings = {}, {}
    for client in front['clients']:
        path = private/(client['id']+'.json')
        pkg.save(path, dict(client_id=client['id'], api_key=client['api_key'], connect_host='127.0.0.1',
            base_url=f'https://{front["server_name"]}:{TLS_PORT}/v1', ca_file=str(cert),
            model='qwen3.8-flash-next', protocol_sha256=protocol['protocol_sha256'],
            template_sha256=protocol['template']['reference_sha256']))
        _, binding = transport.profile(path)
        for label in ('old', 'new'):
            profiles[client['id']+'-'+label] = dict(path=str(path), sha256=pkg.sha(path))
            bindings[client['id']+'-'+label] = binding
    previous = pkg.read(ROOT/'.q4t-work/serve-task-operations-20261104/retry-01/plan.json')
    versions = [dict(label='new', binary=str(ROOT/'build/q4t'),
        binary_sha256=pkg.sha(ROOT/'build/q4t'), sidecar=str(ROOT/'build/q4t.release.json'),
        sidecar_sha256=pkg.sha(ROOT/'build/q4t.release.json'), protocol=protocol['protocol_sha256'])]
    plan = dict(root=str(ROOT), limits=LIMITS, gpu_window_seconds=GPU_WINDOW,
        port=PORT, tls_port=TLS_PORT, versions=versions, gateway=gateway,
        frontend=str(private/'gateway.json'), policy=str(private/'clients.json'),
        profiles=profiles, bindings=bindings, historical=historical,
        certificate=str(cert), key=str(key), model_dir=previous['model_dir'],
        model_inputs=previous['model_inputs'], runtime=pkg.read(release['tested_source_manifest']),
        preserved=previous['preserved'], day_plans=previous['day_plans'],
        local={str(p):pkg.sha(p) for p in (private/'gateway.json', private/'clients.json', cert, key)},
        source_files=sources(), source_commit=subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        receipts={name:pkg.sha(BASE/name/'result.json') for name in
            ('reproduction', 'cpu', 'ca-analysis', 'cpu-repair', 'cpu-driver', 'cpu-driver-final')},
        historical_archive=history.release(8)['evidence'], historical_archive_sha256=history.release(8)['archive_sha256'],
        network_probes=12, model_starts=1, complete_responses=4, real_tools=2,
        performance_acceptance=False, long_run=False)
    pkg.save(PLAN, plan)
    check()
    return dict(prepared=True, model_starts=0, http_requests=0)


def check():
    plan = pkg.read(PLAN)
    pkg.require(plan['root'] == str(ROOT) and plan['limits'] == LIMITS and
        plan['gpu_window_seconds'] == GPU_WINDOW and (plan['port'], plan['tls_port']) ==
        (PORT, TLS_PORT) and plan['network_probes'] == 12, 'stage bounds changed')
    for name, digest in {**plan['source_files'], **plan['runtime']}.items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'frozen source changed: '+name)
    for name, digest in {**plan['preserved'], **plan['local']}.items():
        pkg.require(pkg.sha(name) == digest, 'preserved file changed')
    for name in plan['day_plans']:
        pkg.require(not (Path(name).parent/'execution').exists(), 'user day executed')
    for name, digest in plan['model_inputs'].items():
        pkg.require(pkg.sha(Path(plan['model_dir'])/name) == digest, 'model metadata changed')
    for row in plan['versions']:
        pkg.require(pkg.sha(row['binary']) == row['binary_sha256'] and
                    pkg.sha(row['sidecar']) == row['sidecar_sha256'], 'default changed')
    for name, digest in plan['receipts'].items():
        pkg.require(pkg.sha(BASE/name/'result.json') == digest, 'CPU evidence changed')
    pkg.require(pkg.sha(ROOT/plan['historical_archive']) == plan['historical_archive_sha256'], 'history changed')
    for name, digest in plan['historical']['sources'].items():
        pkg.require(pkg.sha(HISTORICAL/name) == digest, 'historical runtime changed')
    for name, row in plan['profiles'].items():
        pkg.require(pkg.sha(row['path']) == row['sha256'] and
            transport.profile(row['path'])[1] == plan['bindings'][name], 'profile changed')
    prior.s.g.check(plan['gateway']['package'], plan['gateway']['manifest_sha256'])
    return plan


class Run(prior.Run):
    def known(self, client, tokens):
        # alpha exercises the registered prior app; beta exercises the repaired
        # transport for both tool generation and final answer.
        original = prior.HISTORICAL
        try:
            if client == 'beta':
                prior.HISTORICAL = ROOT
            super().known(client, tokens)
        finally:
            prior.HISTORICAL = original

    def resume(self, client, tokens):
        if client == 'alpha':
            denied = self.cli(client, 'new', 'unmigrated', 'resume', expected=1)
            pkg.require(denied['code'] == 'runtime_changed', 'unmigrated app accepted')
            path = BASE/'plans/alpha-migration.json'
            p = self.cli(client, 'new', 'plan-migration', 'plan-migration', '--plan', path,
                         '--reason', 'reviewed absolute network deadline repair')
            moved = self.cli(client, 'new', 'applied', 'apply-migration', '--plan', path,
                             '--plan-sha256', p['plan_sha256'])
            pkg.require(moved['no_execution'] and moved['requests'] == moved['saved_tool_results'] == 1,
                        'source migration executed business')
            duplicate = self.cli(client, 'new', 'duplicate-apply', 'apply-migration', '--plan', path,
                                 '--plan-sha256', p['plan_sha256'])
            pkg.require(duplicate['already_applied'], 'migration duplicate')
        final = self.cli(client, 'new', 'completed', 'resume', generation=True)
        pkg.require(final['state'] == 'COMPLETED' and final['requests'] == 2 and
            final['tool_attempts'] == final['saved_tool_results'] == 1 and final['max_tokens'] == tokens and
            'Qwen4ExpForConditionalGeneration' in final['answer'] and
            '[docs/MODEL.md:L1-L24]' in final['answer'], 'actual answer/budget/citation')
        duplicate = self.cli(client, 'new', 'duplicate-submit', 'submit', '--question', prior.QUESTION,
            '--max-requests', 2, '--max-tools', 1, '--max-tokens', tokens)
        pkg.require(duplicate == dict(final, created=False), 'duplicate execution')
        with store.open_task(TASKS, store.task_name(client, 'migration-query')) as ledger:
            pkg.save(OUT/(client+'-ledger.json'), dict(meta=ledger.meta, events=ledger.events(),
                                                      database_sha256=pkg.sha(ledger.path/'ledger.db')))
            shutil.copytree(ledger.path, OUT/(client+'-completed-task'))

    def business(self):
        deadline_probes.execute(OUT/'network', self.plan['profiles']['alpha-new']['path'],
            self.plan['certificate'], self.plan['key'], HISTORICAL)
        store.init(TASKS, 2)
        (BASE/'plans').mkdir(mode=0o700)
        self.launch(0)
        self.known('alpha', 512)
        self.known('beta', 64)
        self.resume('alpha', 512)
        self.resume('beta', 64)
        metrics = self.service.idle('final-idle')
        pkg.save(OUT/'final-metrics.json', metrics)
        pkg.require(metrics['q4t_requests_success_total'] == 4 and
            metrics['q4t_structured_workers_started_total'] ==
            metrics['q4t_structured_workers_reaped_total'] == 4, 'generation/grammar totals')
        self.service.stop(True)
        self.service.children.reap(OUT/'new')

    def execute(self):
        pkg.require(not (OUT/'begin.json').exists(), 'preserve execution')
        prior.h.require_guard(OUT)
        pkg.save(OUT/'begin.json', dict(monotonic=self.started, unix_seconds=time.time()))
        result = dict(status='FAILED', performance_acceptance=False, long_run=False)
        def interrupted(sig, _):
            raise RuntimeError('validation interrupted: '+str(sig))
        handlers = {sig:signal.signal(sig, interrupted) for sig in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)}
        signal.setitimer(signal.ITIMER_REAL, GPU_WINDOW-40)
        try:
            prior.s.a.require_idle(OUT/'gpu-before.json')
            for port in (PORT, TLS_PORT):
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', port))
            self.business()
            check()
            result.update(status='PASS_TASK_DEADLINE_SHORT', network_probes=12,
                          model_starts=1, complete_responses=4, real_tools=2)
        except BaseException as error:
            result.update(status='ENV_ABORT' if 'ENV_ABORT' in str(error) else 'FAILED', error=repr(error))
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            errors = []
            for service in reversed(self.services):
                for connection in service.sockets:
                    try: connection.close()
                    except BaseException as error: errors.append(repr(error))
                try: service.children.cleanup()
                except BaseException as error: errors.append(repr(error))
            try:
                pkg.require(not errors, 'owned cleanup failed: '+repr(errors))
                prior.s.a.require_idle(OUT/'gpu-final.json')
                pkg.require(time.monotonic()-self.started <= GPU_WINDOW, 'lease window exceeded')
                result['cleanup_confirmed'] = True
            finally:
                for sig, handler in handlers.items(): signal.signal(sig, handler)
                result.update(counts=self.counts, seconds=time.monotonic()-self.started)
                pkg.save(OUT/'result.json', result)


def guard_command():
    return [sys.executable, '-B', str(ROOT/'tools/evalscope/gpu_exclusive.py'),
            '--output', str(OUT/'lease'), '--', sys.executable, '-B', str(Path(__file__).resolve()), 'run']


def guarded():
    check()
    pkg.require(not OUT.exists(), 'preserve guarded execution')
    with (BASE/'run.stdout').open('xb') as stdout, (BASE/'run.stderr').open('xb') as stderr:
        return subprocess.run(guard_command(), cwd=ROOT, stdout=stdout, stderr=stderr).returncode


for name in ('BASE', 'ATTEMPT', 'PLAN', 'OUT', 'HISTORICAL', 'TASKS', 'PORT', 'TLS_PORT', 'LIMITS', 'GPU_WINDOW'):
    setattr(prior, name, globals()[name])

if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'check', 'run', 'guard'))
    action = parser.parse_args().action
    if action == 'prepare': print(json.dumps(prepare()))
    elif action == 'check': check(); print('checked; no HTTP/model')
    elif action == 'guard': raise SystemExit(guarded())
    else: Run(check()).execute()
