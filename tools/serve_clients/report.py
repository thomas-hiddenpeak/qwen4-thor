"""Audit all client-isolation attempts and archive public evidence losslessly."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_clients import cpu, short as s, handoff as h, cache_correction as fix

BASE = cpu.BASE
DEST = ROOT/'docs/evidence/serve-clients-20261104'
RUNS = ('short', 'handoff-short', 'cache-short')


def evaluate(data):
    read = lambda name: json.loads(data[name])
    text = lambda name: data[name].decode()
    s.require(not read('build-v1/result.json')['passed'], 'original link failure missing')
    for name in ('build-v2', 'handoff-build'):
        value = read(name+'/result.json')
        s.require(value['passed'] and value['zero_warnings'] and value['sources_unchanged'], 'build gate')
    s.require(read('cpu-v1/result.json')['passed'] and
              read('handoff-cpu/result.json')['passed'], 'host gate')
    for folder, name, count in [('cpu-v1', 'host-off.stdout', 280),
            ('cpu-v1', 'host-gate/run.log', 246), ('cpu-v1', 'structured.stdout', 9),
            ('handoff-cpu', 'targeted-off.stdout', 29), ('handoff-cpu', 'targeted-on.stdout', 29)]:
        s.require(f'{count} tests, {count} passed, 0 failed, 0 skipped' in text(folder+'/'+name), 'host count')
    s.require(read('cache-driver-cpu/result.json')['passed'] and
              read('cache-driver-cpu/result.json')['tests'] == 4, 'cache driver gate')
    windows = {}
    for name, expected in zip(RUNS, ('COMMAND_FAILED', 'COMMAND_FAILED', 'COMPLETED')):
        result = read(name+'/result.json')
        events = [json.loads(line) for line in data[name+'/lease/events.jsonl'].splitlines()]
        start = [row for row in events if row['event'] == 'lease_acquired']
        end = [row for row in events if row['event'] == 'lease_released']
        observations = [row for row in events if row['event'] in ('monitor', 'preflight')]
        s.require(len(start) == len(end) == 1 and all(not row['conflicts'] for row in observations), 'guard conflict')
        s.require(read(name+'/lease/exit.json')['status'] == end[0]['status'] == expected, 'guard terminal')
        elapsed = end[0]['monotonic']-start[0]['monotonic']
        s.require(0 < elapsed < 600 and result['seconds'] < 600, 'short deadline')
        s.require(all(result['counts'][key] <= result['limits'][key] for key in result['counts']), 'attempt budget')
        windows[name] = dict(status=expected, seconds=elapsed, observations=len(observations))
    s.require(read('short/result.json')['status'] == 'FAILED' and
        'project-read' in read('short/result.json')['error'] and
        read('short/project-read-http.json')['status'] == 429, 'original handoff failure')
    s.require(read('handoff-short/result.json')['status'] == 'FAILED' and
        'cache namespace/priority mismatch' in read('handoff-short/result.json')['error'], 'original cache failure')
    s.require(read('cache-short/result.json')['status'] == 'PASS_BOUNDED_CLIENT_CACHE' and
        read('cache-short/result.json')['cleanup_confirmed'], 'remaining cache result')
    for folder, count in [('short', 27), ('handoff-short', 2)]:
        s.require(f'{count} tests, {count} passed, 0 failed, 0 skipped' in
                  text(folder+'/lifecycle/stdout.log'), 'native HTTP gate')
    errors = {'missing-gateway':(401, 'client_authentication_failed'),
        'output-chat':(400, 'client_output_limit_exceeded'),
        'output-preflight':(400, 'client_output_limit_exceeded'),
        'prompt-chat':(400, 'client_prompt_limit_exceeded'),
        'prompt-preflight':(400, 'client_prompt_limit_exceeded')}
    for label, (status, code) in errors.items():
        s.require(read('short/'+label+'-http.json')['status'] == status and
                  read('short/'+label+'-wire.bin')['error']['code'] == code, 'input policy error')
    s.require(read('short/beta-default-wire.bin')['output']['requested_tokens'] == 64 and
        read('short/forged-client-wire.bin')['client_limits']['max_output_tokens'] == 512, 'trusted identity/default budget')
    for folder in ('short', 'handoff-short'):
        for label, status, code in [('cross-client-cancel',404,'request_not_found'),
                                   ('alpha-quota',429,'client_capacity_exhausted')]:
            s.require(read(folder+'/'+label+'-http.json')['status'] == status and
                      read(folder+'/'+label+'-wire.bin')['error']['code'] == code, 'quota/cancel isolation')
        s.require(read(folder+'/own-cancel-http.json')['status'] == 202 and
                  read(folder+'/alpha-control-http.json')['status'] == 200, 'control lane')
        body = read(folder+'/cancel-alpha-request.json')['body']
        s.cancelled_wire(data[folder+'/cancel-alpha-wire.bin'], body['request_id'])
    successes, timings = {}, {}
    for name, raw in data.items():
        if name.split('/')[0] not in RUNS or not name.endswith('-response.json'):
            continue
        prefix = name[:-len('-response.json')]
        body = read(prefix+'-request.json')['body']
        receipt = read(prefix+'-http.json')
        s.require(receipt['status'] == 200, 'successful response without HTTP 200')
        value = (s.a.read_sse if body.get('stream') else s.a.read_json)(io.BytesIO(data[prefix+'-wire.bin']))
        s.require(value.id == body['request_id'] and value.model == body['model'], 'wire identity')
        successes[prefix] = value
        if 'elapsed' in read(name):
            timings[prefix] = read(name)['elapsed']
    s.require(len(successes) == 18, 'successful generation accounting')
    for name, value in successes.items():
        if name.endswith(('legacy-quality', 'scoped-quality')):
            s.require(value.message['content'].strip() == '710003', 'ordinary quality')
        elif name.endswith('scoped-structured'):
            s.require(json.loads(value.message['content']) == {'ok': True}, 'structured output')
        elif '/cache-' in name and not name.endswith('alpha-seed') or '/shared-' in name:
            s.require(value.message['content'].strip() == '43', 'cache answer quality')
    tool = read('handoff-short/project-tool-result.json')
    output = json.loads(tool['content'])
    s.a.validate_answer(successes['handoff-short/project-answer'].message['content'],
                        {output['source']['citation']}, 1)
    s.require('Qwen4ExpForConditionalGeneration' in
        successes['handoff-short/project-answer'].message['content'], 'project answer')
    private = {name: successes['handoff-short/cache-'+name].usage['prompt_tokens_details']['cached_tokens']
               for name in ('beta-isolated', 'alpha-private', 'alpha-shared', 'beta-shared')}
    s.require(private == {'beta-isolated':0,'alpha-private':8234,'alpha-shared':0,'beta-shared':0}, 'private cache evidence')
    for label in ('cache-alpha-seed', 'cache-beta-isolated', 'cache-alpha-shared', 'cache-beta-shared'):
        s.require(read('handoff-short/'+label+'-request.json')['body'].get('prefix_cache_write_tokens',0) == 0,
                  'original read-only fixture changed')
    shared = read('cache-short/shared-cache.json')
    fix.check_cache(shared)
    for label, expected in zip(fix.LABELS, (8192,8192,0,0)):
        s.require(read('cache-short/shared-'+label+'-request.json')['body']['prefix_cache_write_tokens'] == expected,
                  'explicit seed/read receipt')
        s.require(successes['cache-short/shared-'+label].usage['prompt_tokens_details']['cached_tokens'] == shared[label],
                  'shared wire usage')
    final = read('cache-short/final-metrics.json')
    s.require(all(final[key] == 0 for key in s.a.EMPTY) and final['q4t_gpu_healthy'] == 1 and
              final['q4t_seq_slots_free'] == 1, 'final ownership/health')
    for name in ('scoped-model', 'nginx'):
        s.require(read('cache-short/'+name+'/reaped.json')['returncode'] == 0, 'normal cleanup')
    s.require(len(read('cache-short/custody.json')['retained']) == 2, 'child custody')
    post = read('postflight/result.json')
    s.require(post['passed'] and post['days_unchanged'] and post['default_unchanged'] and
              len(post['owned']) == 9 and all(row['exited'] for row in post['owned']), 'postflight')
    counts = {key: sum(read(name+'/result.json')['counts'][key] for name in RUNS)
              for key in ('starts','controls','generation_attempts')}
    s.require(counts == dict(starts=4, controls=20, generation_attempts=26), 'complete attempt accounting')
    return dict(status='ACCEPT_BOUNDED_CLIENT_ISOLATION', counts=counts,
        successful_generations=18, cancelled_generations=2, pre_generation_refusals=6,
        real_project_tools=1, native_http_distinct=27, native_http_executions=29,
        cpu=dict(public=280, required_before_repair=246, new_handoff=3,
                 targeted_each_mode=29, structured=9, storage_checkpoints=28, packages=70,
                 original_driver=4, cache_driver=4),
        original_link_failure_preserved=True, original_handoff_failure_preserved=True,
        original_cache_fixture_failure_preserved=True, private_cache=private, shared_cache=shared,
        windows=windows, request_seconds=timings, candidate=read('cache-plan.json')['binary'],
        candidate_sha256=read('handoff-build/result.json')['binaries'][str(h.ENGINE.relative_to(ROOT))+'/q4t'],
        protocol_sha256=read('cache-plan.json')['protocol'], long_run=False,
        default_installed=False, managed_e2e=False, performance_acceptance=False)


def postflight():
    out = BASE/'postflight'
    out.mkdir()
    s.OUT, s.LIMITS = fix.OUT, fix.LIMITS
    plan = s.prepared_identity(s.pkg.read(fix.PLAN))
    s.a.require_idle(out/'gpu-first.json')
    start = time.monotonic()
    unit = 'soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'
    mask = Path.home()/'.config/systemd/user'/unit
    s.require(mask.is_symlink() and os.readlink(mask) == '/dev/null', 'SOAR mask missing')
    result = subprocess.run(['systemctl','--user','show',unit,'-p','MainPID','-p','ActiveState','-p','LoadState'],
                            capture_output=True,text=True,timeout=10,check=True)
    s.require(all(x in result.stdout for x in ('MainPID=0','ActiveState=inactive','LoadState=masked')), 'SOAR state')
    (out/'soar-unit.txt').write_text(result.stdout)
    containers = subprocess.run(['sudo','-n','docker','ps','--format','{{.Names}}'],capture_output=True,
                                text=True,timeout=10,check=True)
    s.require(not containers.stdout.strip(), 'unexpected containers; do not interfere')
    (out/'containers.txt').write_text(containers.stdout)
    time.sleep(max(0,10-(time.monotonic()-start)))
    owned=[]
    for name in RUNS:
        for path in sorted((BASE/name).glob('*/identity.json')):
            identity=s.pkg.read(path)
            stat=Path(f"/proc/{identity['pid']}/stat")
            same=stat.exists() and int(stat.read_text().rsplit(')',1)[1].split()[19])==identity['start_ticks']
            owned.append(dict(path=str(path),pid=identity['pid'],start_ticks=identity['start_ticks'],exited=not same))
    s.require(len(owned)==9 and all(row['exited'] for row in owned), 'owned process survived')
    s.a.require_idle(out/'gpu-last.json')
    lock=s.a.guard.common_lock(); info=lock.stat()
    s.require((info.st_dev,info.st_ino)==(66305,3294067) and s.a.lock_available(lock), 'lock not released')
    s.prepared_identity(plan)
    cpu.save(out/'result.json',dict(passed=True,owned=owned,after_seconds=time.monotonic()-start,
        lock_device=info.st_dev,lock_inode=info.st_ino,lock_available=True,
        days_unchanged=True,default_unchanged=True,new_model_or_http=0))


def collect():
    data={}
    for name in ('build-v1','build-v2','cpu-v1','driver-cpu','handoff-build','handoff-cpu',
                 'cache-driver-cpu',*RUNS,'postflight','report-audit'):
        for path in sorted((BASE/name).rglob('*')):
            if path.is_file() and not path.is_symlink() and not {'sessions','prefixes'} & set(path.parts):
                data[str(path.relative_to(BASE))]=path.read_bytes()
    for path in (s.PLAN,h.PLAN,fix.PLAN):
        data[path.name]=path.read_bytes()
    data['archiver/report.py']=Path(__file__).read_bytes()
    policy=s.pkg.read(BASE/'short-private/clients.json')
    front=s.pkg.read(BASE/'short-private/gateway.json')
    secrets=[policy['gateway_token'],*(row['api_key'] for row in front['clients'])]
    for name,raw in data.items():
        pem = [b'-----BEGIN '+kind+b'PRIVATE KEY-----' for kind in (b'', b'RSA ', b'EC ', b'ENCRYPTED ')]
        s.require(all(value.encode() not in raw for value in secrets) and not any(marker in raw for marker in pem),
                  'private material in public evidence: '+name)
    local={}
    for name in ('build-v2','handoff-build'):
        for path,digest in s.pkg.read(BASE/name/'result.json')['binaries'].items():
            s.require(cpu.sha(ROOT/path)==digest,'local binary changed')
            local[path]=dict(bytes=(ROOT/path).stat().st_size,sha256=digest)
    return data,local


def archive():
    data,local=collect()
    result=evaluate(data)
    DEST.mkdir(parents=True)
    files={name:dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()) for name,raw in sorted(data.items())}
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w') as target:
        for name,raw in sorted(data.items()):
            entry=tarfile.TarInfo(name)
            entry.size,entry.mode,entry.mtime=len(raw),0o600,0
            target.addfile(entry,io.BytesIO(raw))
    packed=gzip.compress(buffer.getvalue(),mtime=0)
    (DEST/'records.tar.gz').write_bytes(packed)
    cpu.save(DEST/'manifest.json',dict(schema=1,files=files,local_identities=local,
        archive=dict(bytes=len(packed),sha256=hashlib.sha256(packed).hexdigest())))
    cpu.save(DEST/'result.json',result)
    return dict(records=len(data),local_identities=len(local),compressed_bytes=len(packed),result=result)


def verify():
    manifest=s.pkg.read(DEST/'manifest.json')
    packed=(DEST/'records.tar.gz').read_bytes()
    s.require(len(packed)==manifest['archive']['bytes'] and
              hashlib.sha256(packed).hexdigest()==manifest['archive']['sha256'],'archive identity')
    data={}
    with tarfile.open(fileobj=io.BytesIO(packed),mode='r:gz') as source:
        for member in source:
            s.require(member.isfile() and member.name not in data and member.name in manifest['files'], 'archive member')
            raw=source.extractfile(member).read(); identity=manifest['files'][member.name]
            s.require(len(raw)==identity['bytes'] and hashlib.sha256(raw).hexdigest()==identity['sha256'], 'record hash')
            data[member.name]=raw
    s.require(data.keys()==manifest['files'].keys() and evaluate(data)==s.pkg.read(DEST/'result.json'), 'offline evidence mismatch')
    return dict(passed=True,records=len(data),local_identities=len(manifest['local_identities']),new_model_or_http=0)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('postflight','archive','verify'))
    args=parser.parse_args()
    print(json.dumps(globals()[args.action](),ensure_ascii=False))
