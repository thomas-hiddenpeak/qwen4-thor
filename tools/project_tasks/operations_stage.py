"""SRV-11i: exact saved-tool handoff from fc63db28 to ddfba8c0."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import protocol_stage as prior
from tools.project_tasks import engine, transport, protocol, migration_fixtures as history
from tools.project_tasks import operations_review as review
from tools.serve_service import package as pkg

BASE = review.BASE
PLAN = BASE/'plan.json'
OUT = BASE/'execution'
HISTORICAL = BASE/'historical-runtime'
TASKS = HISTORICAL/'.q4t-work/tasks'
PORT, TLS_PORT = 18134, 18454
LIMITS = dict(starts=2, generation_attempts=4, controls=80, seconds=300)
GPU_WINDOW = 270
CPU_SOURCES = sorted(set((*engine.RUNTIME_FILES,
    'tools/project_tasks/test_tasks.py', 'tools/project_tasks/test_protocol.py',
    'tools/project_tasks/test_operations_protocol.py',
    'tools/project_tasks/operations_review.py',
    'tools/project_tasks/operations_stage.py',
    'tools/project_tasks/operations_cpu.py',
    'tools/project_tasks/operations_driver_cpu.py',
    'tools/project_tasks/protocol_stage.py',
    'tools/project_tasks/protocol_driver_cpu.py',
    'tools/project_tasks/migration_worker.py',
    'tools/project_tasks/migration_fixtures.py',
    'docs/SERVE_TASK_OPERATIONS_2026-11-04.md')))


def sources():
    names = set(subprocess.check_output(['git', 'ls-files', 'tools', 'cmake'],
                                        cwd=ROOT, text=True).splitlines())
    names.update(CPU_SOURCES)
    names.update(('docs/MODEL.md', 'docs/serve-config-v1.schema.json'))
    return {n: pkg.sha(ROOT/n) for n in sorted(names)}


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists() and not HISTORICAL.exists(),
                'preserve previous execution')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
                'commit reviewed stage first')
    cpu = pkg.read(BASE/'cpu/result.json')
    pkg.require(cpu['passed'] and not cpu['errors'] and not cpu['failures'] and
                cpu['tests_run'] >= 20, 'targeted CPU required')
    for name, digest in cpu['sources'].items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'CPU source drift: '+name)
    historical = history.extract_runtime(7, HISTORICAL)
    private = HISTORICAL/'.q4t-work/private'
    private.mkdir(mode=0o700, parents=True)
    cert, key = prior.s.certificate(private)
    front = prior.s.example(TLS_PORT, PORT)
    front.update(per_client_inflight=4, global_inflight=8,
                 per_client_rps=100, burst=128)
    policy = dict(version=1, gateway_token=prior.s.secrets.token_hex(32), clients=[
        dict(id='alpha', max_inflight=1, max_control_inflight=2,
             max_prompt_tokens=12000, max_output_tokens=512, max_body_bytes=131072),
        dict(id='beta', max_inflight=1, max_control_inflight=2,
             max_prompt_tokens=9000, max_output_tokens=64, max_body_bytes=65536)])
    pkg.save(private/'gateway.json', front)
    pkg.save(private/'clients.json', policy)
    gateway = prior.s.g.prepare(private/'gateway.json', pkg.read(prior.s.VENDOR)['binary'],
        cert, key, private/'gateway', private/'clients.json')
    profiles, bindings, versions = {}, {}, []
    for label, directory in (('old', review.OLD), ('new', ROOT/'build')):
        sidecar = pkg.read(directory/'q4t.release.json')
        declaration = sidecar['chat_protocol']
        pkg.require(pkg.sha(directory/'q4t') == sidecar['binary_sha256'], 'release pair mismatch')
        versions.append(dict(label=label, binary=str(directory/'q4t'),
            binary_sha256=pkg.sha(directory/'q4t'), sidecar=str(directory/'q4t.release.json'),
            sidecar_sha256=pkg.sha(directory/'q4t.release.json'),
            protocol=declaration['protocol_sha256']))
        for client in front['clients']:
            name = client['id']+'-'+label
            path = private/(name+'.json')
            pkg.save(path, dict(client_id=client['id'], api_key=client['api_key'],
                connect_host='127.0.0.1', base_url=f'https://{front["server_name"]}:{TLS_PORT}/v1',
                ca_file=str(cert), model='qwen3.8-flash-next',
                protocol_sha256=declaration['protocol_sha256'],
                template_sha256=declaration['template']['reference_sha256']))
            _, bindings[name] = transport.profile(path)
            profiles[name] = dict(path=str(path), sha256=pkg.sha(path))
    transition = protocol.reviewed(bindings['alpha-old'], bindings['alpha-new'])
    original = pkg.read(ROOT/'.q4t-work/serve-operations-listener-20261104/package-business-fix/plan.json')
    current = pkg.read(ROOT/'build/q4t.release.json')
    pkg.verify_declarations(current, required=True)
    plan = dict(root=str(ROOT), limits=LIMITS, gpu_window_seconds=GPU_WINDOW,
        port=PORT, tls_port=TLS_PORT, versions=versions, gateway=gateway,
        frontend=str(private/'gateway.json'), policy=str(private/'clients.json'),
        profiles=profiles, bindings=bindings, protocol_review=transition,
        historical=historical, model_dir=original['model_dir'],
        model_inputs=original['model_inputs'], runtime=pkg.read(current['tested_source_manifest']),
        preserved=original['preserved'],
        local={str(p): pkg.sha(p) for p in (private/'gateway.json', private/'clients.json', cert)},
        source_files=sources(), source_commit=subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        cpu_sha256=pkg.sha(BASE/'cpu/result.json'),
        historical_archive=review.HISTORY, historical_archive_sha256=review.HISTORY_SHA,
        performance_acceptance=False, long_run=False)
    plan['day_plans'] = [n for n in plan['preserved'] if n.endswith('/day/plan.json')]
    pkg.require(len(plan['day_plans']) == 3, 'three retained user day plans')
    pkg.save(PLAN, plan)
    check()
    return dict(prepared=True, models_started=0, listeners_started=0)


def check():
    plan = pkg.read(PLAN)
    pkg.require(plan['root'] == str(ROOT) and plan['limits'] == LIMITS and
        plan['gpu_window_seconds'] == GPU_WINDOW and (plan['port'], plan['tls_port']) ==
        (PORT, TLS_PORT), 'stage scope changed')
    for name, digest in {**plan['source_files'], **plan['runtime']}.items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'frozen source changed: '+name)
    for name, digest in {**plan['preserved'], **plan['local']}.items():
        pkg.require(pkg.sha(name) == digest, 'preserved artifact changed')
    for name in plan['day_plans']:
        pkg.require(not (Path(name).parent/'execution').exists(), 'user day executed')
    pkg.require(pkg.sha(BASE/'cpu/result.json') == plan['cpu_sha256'], 'CPU receipt changed')
    for name, digest in plan['model_inputs'].items():
        pkg.require(pkg.sha(Path(plan['model_dir'])/name) == digest, 'model input changed')
    for row in plan['versions']:
        pkg.require(pkg.sha(row['binary']) == row['binary_sha256'] and
                    pkg.sha(row['sidecar']) == row['sidecar_sha256'], 'release changed')
    pkg.require(pkg.sha(ROOT/plan['historical_archive']) == plan['historical_archive_sha256'],
                'historical source archive changed')
    pkg.require(pkg.sha(ROOT/plan['protocol_review']['evidence']) ==
                plan['protocol_review']['archive_sha256'], 'review evidence changed')
    for name, digest in plan['historical']['sources'].items():
        pkg.require(pkg.sha(HISTORICAL/name) == digest, 'historical runtime changed')
    for name, row in plan['profiles'].items():
        pkg.require(pkg.sha(row['path']) == row['sha256'] and
            transport.profile(row['path'])[1] == plan['bindings'][name], 'profile changed')
    prior.s.g.check(plan['gateway']['package'], plan['gateway']['manifest_sha256'])
    return plan


class Run(prior.Run):
    def launch(self, index):
        service = super().launch(index)
        caps = service.request('migration-capabilities', path='/v1/capabilities', method='GET')
        pkg.require(caps['protocol_source_sha256'] == self.plan['versions'][index]['protocol'],
                    'observed protocol mismatch')
        pkg.save(OUT/('capabilities-'+str(index)+'.json'), caps)
        if index:
            before = pkg.read(OUT/'capabilities-0.json')
            changed = sorted(n for n in caps.keys() | before.keys()
                             if caps.get(n) != before.get(n))
            pkg.require(changed == ['http', 'observability', 'protocol_source_sha256'],
                        'unreviewed public capability change')
            pkg.save(OUT/'capability-difference.json', dict(changed=changed,
                old_version=before['observability']['version'],
                new_version=caps['observability']['version']))
        return service

    def cli(self, client, version, label, action, *extra, **kwargs):
        if action == 'plan-protocol' and label == 'plan-protocol':
            extra = tuple('explicit reviewed SRV-12g public protocol upgrade'
                if value == 'explicit reviewed SRV-01c upgrade' else value for value in extra)
        return super().cli(client, version, label, action, *extra, **kwargs)


def configure():
    # Reuse the accepted bounded lifecycle algorithm in this process only.
    # Never edit the old driver or its frozen records.
    for name in ('BASE', 'PLAN', 'OUT', 'HISTORICAL', 'TASKS', 'PORT', 'TLS_PORT',
                 'LIMITS', 'GPU_WINDOW'):
        setattr(prior, name, globals()[name])
    prior.ATTEMPT = BASE
    prior.check = check


configure()
if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'check', 'run'))
    action = parser.parse_args().action
    if action == 'prepare': print(json.dumps(prepare()))
    elif action == 'check': check(); print('checked; no model or HTTP')
    else: Run(check()).execute()
