"""Lossless public receipts and offline reconstruction of SRV-01c acceptance."""
import argparse
from dataclasses import asdict
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_error_params import cpu
from tools.examples import chat_client

BASE=cpu.BASE
DEST=ROOT/'docs/evidence/serve-error-params-20261104'
READERS=('tools/serve_error_params/report.py','tools/serve_error_params/cpu.py',
         'tools/serve_error_params/cases.py','tools/examples/chat_client.py')


def frozen_plan():
    """The runtime is unchanged; preserve the original offline reader too."""
    from tools.serve_error_params import stage as s
    s.h.verify_seal(s.PACKAGE)
    plan=cpu.read(s.PACKAGE/'plan.json')
    cpu.require(plan['root']==str(ROOT) and plan['limits']==s.LIMITS,'frozen scope')
    for name,digest in cpu.read(s.PACKAGE/'sources.json').items():
        path=BASE/'offline-original/report.py' if name==READERS[0] else ROOT/name
        cpu.require(cpu.sha(path)==digest,'frozen execution source changed: '+name)
    s.a.verify_hashes(ROOT,cpu.read(s.PACKAGE/'candidate/runtime-source.json'))
    s.a.verify_hashes(plan['model_dir'],plan['model_inputs'])
    for path,digest in {**plan['preserved'],**plan['local']}.items():
        cpu.require(cpu.sha(path)==digest,'preserved artifact changed')
    for path in s.DAYS:cpu.require(not (path.parent/'execution').exists(),'user day executed')
    s.s.g.check(plan['gateway']['package'],plan['gateway']['manifest_sha256'])
    s.h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',plan['candidate_sha256'])
    return plan


def postflight():
    from tools.serve_error_params import stage as s
    out=BASE/'postflight-corrected';out.mkdir()
    plan=frozen_plan()
    s.a.require_idle_gpu(out/'gpu.json')
    lock=s.s.a.guard.common_lock();info=lock.stat()
    cpu.require((info.st_dev,info.st_ino)==(66305,3294067) and s.s.a.lock_available(lock),'original GPU lock not released')
    owners=[]
    for folder in ('scoped-model','nginx'):
        old=cpu.read(BASE/'execution/business'/folder/'identity.json')
        try:current=s.child.custody.proc(old['pid'])
        except FileNotFoundError:current=None
        cpu.require(current is None or current['start_ticks']!=old['start_ticks'],'own process remains')
        owners.append(dict(pid=old['pid'],start_ticks=old['start_ticks'],gone=True))
    for port in (s.PORT,s.TLS_PORT):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            probe.bind(('127.0.0.1',port))
    listeners=subprocess.check_output(['ss','-ltn',f'( sport = :{s.PORT} or sport = :{s.TLS_PORT} )'],text=True)
    (out/'listeners.txt').write_text(listeners)
    cpu.require(len(listeners.splitlines())==1,'listener retained')
    unit='soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'
    raw=subprocess.check_output(['systemctl','--user','show',unit,'-p','LoadState','-p','ActiveState','-p','SubState','-p','MainPID'],text=True)
    (out/'soar-unit.txt').write_text(raw)
    state=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
    cpu.require(state==dict(MainPID='0',LoadState='masked',ActiveState='inactive',SubState='dead'),'recovery service changed')
    containers=subprocess.check_output(['sudo','-n','docker','ps','--format','{{.ID}} {{.Names}}'],text=True)
    (out/'containers.txt').write_text(containers)
    cpu.require(not containers.strip(),'unexpected running container')
    installed=cpu.read(BASE/'execution/finalized.json')
    cpu.require(installed['status']=='DELIVERED','installation not finalized')
    cpu.save(out/'result.json',dict(passed=True,owned=owners,ports_released=[s.PORT,s.TLS_PORT],
        lock=dict(dev=info.st_dev,inode=info.st_ino,released=True),preserved_artifacts=len(plan['preserved']),
        source_runtime=cpu.read(BASE/'package/candidate/runtime-source.json'),
        installed=installed['installed'],long_run=False,
        port_method='SO_REUSEADDR bind plus explicit no-LISTEN observation',
        original_immediate_port_cause='Not observed; first EADDRINUSE remains a failed check',
        analysis_source_sha256=cpu.sha(__file__)))


def evaluate(data):
    read=lambda name:json.loads(data[name])
    require=cpu.require
    require(read('build-01/result.json')['passed'] and read('build-01/result.json')['zero_warnings'],'build gate')
    first=read('cpu-01/result.json')
    require(not first['passed'] and {k for k,v in first['checks'].items() if not v}=={'generated'},'original generated failure missing')
    require(b'generated contract drift' in data['cpu-01/generated.stderr'],'original generation failure lost')
    require(read('cpu-finish/result.json')['passed'] and read('cpu-finish/generated-exit.json')['returncode']==0,'metadata correction gate')
    for path,count in [('cpu-01/public-host.stdout',287),('cpu-01/host-gate/run.log',253),('cpu-01/structured.stdout',9)]:
        require(f'{count} tests, {count} passed, 0 failed, 0 skipped'.encode() in data[path],'native count')
    require(read('driver-cpu/result.json')['passed'] and read('driver-cpu-v2/result.json')['passed'] and
        read('driver-cpu-v3/result.json')['passed'] and read('driver-cpu-v3/result.json')['tests']==4,'driver gate')
    cases=read('cpu-01/cases.json');before=[json.loads(x) for x in data['cpu-01/baseline.stdout'].splitlines()]
    after=[json.loads(x) for x in data['cpu-01/candidate.stdout'].splitlines()]
    require(len(cases)==len(before)==len(after)==91,'differential records')
    changed=accepted=0
    for case,old,new in zip(cases,before,after):
        require(old['accepted']==new['accepted']==case['accepted'],'acceptance changed')
        if case['accepted']:
            require(old==new,'prompt/grammar changed');accepted+=1
        else:
            a,b=old['response']['error'],new['response']['error']
            require(b['param']==case['param'] and all(a[k]==b[k] for k in ('code','type','message')),'error contract')
            changed+=a['param']!=b['param']
    require(accepted==14 and changed==77,'differential coverage')
    plan=read('package/plan.json');execution=read('execution/execution.json')
    require(execution['status']=='HEALTH_CHECKED_PENDING_GUARD' and
        read('execution/finalized.json')['status']=='DELIVERED','delivery state')
    result=read('execution/business/result.json')
    require(result['status']=='PASS_BOUNDED_ERROR_PARAMS' and result['successes']==6 and
            result['negatives']==36 and result['tools']==2,'real business result')
    require(all(result['counts'][k]<=plan['limits'][k] for k in result['counts']),'request budget')
    events=[json.loads(x) for x in data['execution/lease/events.jsonl'].splitlines()]
    starts=[r for r in events if r['event']=='lease_acquired'];ends=[r for r in events if r['event']=='lease_released']
    observed=[r for r in events if r['event'] in ('preflight','monitor')]
    require(len(starts)==len(ends)==1 and ends[0]['status']=='COMPLETED' and
            read('execution/lease/exit.json')['status']=='COMPLETED' and observed and
            all(not r['conflicts'] for r in observed),'guard coverage')
    seconds=ends[0]['monotonic']-starts[0]['monotonic']
    require(0<seconds<plan['limits']['seconds'] and result['seconds']<plan['limits']['seconds'],'short duration')
    negatives=read('execution/business/negatives.json');require(len(negatives)==36,'negative receipts')
    expected={r['name']:r['param'] for r in cases}
    for row in negatives:
        label=row['label'];name,kind=label.removeprefix('negative-').rsplit('-',1)
        require(kind in ('json','stream','preflight'),'negative route')
        wire=read('execution/business/'+label+'-wire.bin')['error']
        http=read('execution/business/'+label+'-http.json')
        headers={k.lower():v for k,v in http['headers'].items()}
        require(http['status']==400 and wire['param']==expected[name] and
                wire['code']=='invalid_request' and wire['type']=='invalid_request_error' and
                headers['x-request-id']==label and headers['content-type'].startswith('application/json') and
                headers['x-q4t-gateway-request-id'],'HTTP error wire')
    responses={};usage={}
    for label in ('ordinary','structured','project-alpha-read','project-alpha-answer','project-beta-read','project-beta-answer'):
        prefix='execution/business/'+label
        request=read(prefix+'-request.json')['body'];http=read(prefix+'-http.json')
        require(http['status']==200,'positive HTTP status')
        completion=(chat_client.read_sse if request.get('stream') else chat_client.read_json)(io.BytesIO(data[prefix+'-wire.bin']))
        normalized=read(prefix+'-response.json');normalized.pop('elapsed')
        require(asdict(completion)==normalized and completion.id==request['request_id'] and
            completion.model==request['model'],'wire/result identity')
        require(completion.usage['completion_tokens']<=request['max_tokens'],'output budget')
        responses[label]=completion;usage[label]=completion.usage
    require(responses['ordinary'].message['content'].strip()=='710003' and
            json.loads(responses['structured'].message['content'])=={'ok':True},'plain/strict quality')
    for client in ('alpha','beta'):
        prefix='execution/business/project-'+client
        tools=responses['project-'+client+'-read'].message['tool_calls']
        require(len(tools)==1 and tools[0]['function']['name']=='read_project_record' and
            json.loads(tools[0]['function']['arguments'])==dict(path='docs/MODEL.md',start_line=1,line_count=24),'actual tool call')
        returned=read(prefix+'-tool-result.json');value=json.loads(returned['content'])
        require(value['ok'] and returned['tool_call_id']==tools[0]['id'],'actual tool result')
        answer=responses['project-'+client+'-answer']
        require(answer.finish_reason=='stop' and 'Qwen4ExpForConditionalGeneration' in answer.message['content'] and
                value['source']['citation'] in answer.message['content'],'actual cited answer')
    metrics=read('execution/business/final-metrics.json')
    require(metrics['q4t_requests_success_total']==6 and metrics['q4t_structured_workers_started_total']==
        metrics['q4t_structured_workers_reaped_total']==5,'model/grammar accounting')
    require(b'shutdown complete (0 in-flight remaining)' in data['execution/business/scoped-model/stderr.log'],'drain')
    for name in ('scoped-model','nginx'):
        require(read('execution/business/'+name+'/reaped.json')['returncode']==0,'child reap')
    post=read('postflight-corrected/result.json')
    require(post['passed'] and len(post['owned'])==2 and all(x['gone'] for x in post['owned']) and
            post['lock']==dict(dev=66305,inode=3294067,released=True),'resource cleanup')
    require(read('postflight-corrected/gpu.json')['status']=='OK' and not read('postflight-corrected/gpu.json')['pids'],'GPU retained')
    require(not data['postflight-corrected/containers.txt'].strip() and b'LoadState=masked' in data['postflight-corrected/soar-unit.txt'],'recovery unit/container')
    require(len(data['postflight-corrected/listeners.txt'].splitlines())==1,'listener retained')
    require('Address already in use' in read('postflight-original/observed-failure.json')['error'] and
            'permission denied' in read('postflight-second/observed-failure.json')['error'],'postflight refusals lost')
    require(post['installed']==read('execution/release/binding.json')['new'] and
            post['installed']['q4t']==plan['candidate_sha256'],'installed same ELF')
    return dict(status='ACCEPT_BOUNDED_ERROR_PARAMS',cpu_public=287,cpu_required=253,
        cpu_structured=9,driver_effective=4,differential=91,unchanged_successes=14,
        improved_error_locations=77,http_refusals=36,generations=6,real_tools=2,
        gpu_seconds=seconds,guard_observations=len(observed),counts=result['counts'],usage=usage,
        installed=post['installed'],protocol_sha256=plan['protocol_sha256'],owned_processes_reaped=2,
        original_generated_failure_preserved=True,offline_failures_preserved=True,
        performance_acceptance=False,long_run=False,
        actual_lan_deployment=False,full_physical_ram_protection=False,new_verification_http=0)


def archive():
    data={}
    for name in ('build-01','baseline-source','cpu-01','cpu-finish','driver-cpu','driver-cpu-v2','driver-cpu-v3',
                 'driver-original-source','driver-v2-source','generated-original','postflight-original',
                 'postflight-second','postflight-corrected','offline-original'):
        for path in sorted((BASE/name).rglob('*')):
            if path.is_file():data[str(path.relative_to(BASE))]=path.read_bytes()
    for path in sorted((BASE/'execution').rglob('*')):
        relative=str(path.relative_to(BASE))
        if path.is_file() and path.suffix not in ('.sqlite','.db') and not any(part in ('pair','sessions','prefixes') for part in path.parts):
            data[relative]=path.read_bytes()
    for name in ('plan.json','sources.json','numerical-reuse.json','candidate/runtime-source.json',
                 'candidate/generated/q4t/server/chat_protocol_sources.json','previous/q4t.release.json'):
        data['package/'+name]=(BASE/'package'/name).read_bytes()
    for name in READERS:
        data['source/'+name]=(ROOT/name).read_bytes()
    data['source/docs/serve-input-semantics-v1.json']=(ROOT/'docs/serve-input-semantics-v1.json').read_bytes()
    plan=cpu.read(BASE/'package/plan.json')
    gateway=Path(plan['gateway']['package'])
    data['gateway/manifest.json']=(gateway/'manifest.json').read_bytes()
    data['gateway/certificate.pem']=(gateway/'certificate.pem').read_bytes()
    secrets=[c['api_key'].encode() for c in cpu.read(plan['frontend'])['clients']]
    secrets.append(cpu.read(plan['policy'])['gateway_token'].encode())
    for name,raw in data.items():
        cpu.require(len(raw)<32*1024**2 and not any(secret in raw for secret in secrets) and
                    not re.search(br'(?m)^-----BEGIN (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----\r?$',raw),
                    'private/oversized record: '+name)
    result=evaluate(data)
    DEST.mkdir()
    with (DEST/'records.tar.gz').open('xb') as stream,gzip.GzipFile(fileobj=stream,mode='wb',mtime=0,filename='') as compressed:
        with tarfile.open(fileobj=compressed,mode='w') as tar:
            for name,raw in sorted(data.items()):
                info=tarfile.TarInfo(name);info.size=len(raw);info.mode=0o600;info.mtime=0
                tar.addfile(info,io.BytesIO(raw))
    cpu.save(DEST/'result.json',result)
    cpu.save(DEST/'manifest.json',dict(records={name:dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()) for name,raw in sorted(data.items())},
        archive_sha256=cpu.sha(DEST/'records.tar.gz'),result_sha256=cpu.sha(DEST/'result.json'),
        replay_sources={name:cpu.sha(ROOT/name) for name in READERS},
        local_binaries={str(path):cpu.sha(path) for path in (BASE/'package/candidate/q4t',BASE/'package/previous/q4t',
            Path(cpu.read(gateway/'manifest.json')['nginx']['path']),Path(sys.executable).resolve(),Path('/usr/bin/openssl'))}))
    print(dict(archived=len(data),bytes=(DEST/'records.tar.gz').stat().st_size,result=result['status']))


def verify():
    manifest=cpu.read(DEST/'manifest.json')
    cpu.require(all(cpu.sha(ROOT/p)==v for p,v in manifest['replay_sources'].items()),'offline reader source drift')
    cpu.require(cpu.sha(DEST/'records.tar.gz')==manifest['archive_sha256'] and
                cpu.sha(DEST/'result.json')==manifest['result_sha256'],'archive/result digest')
    with tarfile.open(DEST/'records.tar.gz','r:gz') as tar:
        members=tar.getmembers()
        cpu.require(len(members)==len(manifest['records']) and all(m.isfile() and m.size<32*1024**2 for m in members),'archive file set')
        data={m.name:tar.extractfile(m).read() for m in members}
    cpu.require(set(data)==set(manifest['records']),'archive names')
    for name,raw in data.items():
        cpu.require(manifest['records'][name]==dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()),'record drift: '+name)
    cpu.require(evaluate(data)==cpu.read(DEST/'result.json'),'offline reconstruction differs')
    print(dict(passed=True,records=len(data),new_model=0,new_http=0))


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('postflight','archive','verify'))
    globals()[parser.parse_args().action]()
