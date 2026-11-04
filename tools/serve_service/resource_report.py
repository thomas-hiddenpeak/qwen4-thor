"""Rebuild host limits, kernel faults and real tool evidence without GPU/HTTP."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_service import resource_stage as s,package as pkg
from tools.project_tasks import engine,store

BASE=s.BASE
DEST=ROOT/'docs/evidence/serve-host-envelope-20261104'
require=pkg.require
sha=lambda raw:hashlib.sha256(raw).hexdigest()


def evaluate(data):
    read=lambda name:json.loads(data[name])
    plan=read('plan.json');result=read('execution/result.json')
    for folder,count in (('cpu-01',155),('cpu-fix',1),('cpu-driver',2)):
        value=read(folder+'/result.json')
        require(value['passed'] and value['tests_run']==count and value['errors']==value['failures']==0 and
            value['models_started']==value['listeners_started']==value['gpu_started']==0,'CPU outcome')
        for name,checksum in value['sources'].items():
            require(sha(data[folder+'/source/'+name])==checksum,'CPU source archive')
            if folder!='cpu-01' or name not in ('tools/serve_service/resource_worker.py','tools/serve_service/resource_kernel.py'):
                require(plan['source_files'][name]==checksum,'tested source identity')
    for folder,checksum in plan['receipts'].items():require(sha(data[folder+'/result.json'])==checksum,'plan receipt binding')
    require('TimeoutExpired' in read('kernel-01/high/failure.json')['error'] and
        read('kernel-01/high/failure-cleanup.json')['scope_removed'],'preserve original throttled probe')
    cases=[read('kernel-01/normal/result.json'),*read('kernel-02/result.json')['cases']]
    require([v['case'] for v in cases]==['normal','high','oom','tasks','drift','death'] and
        all(v['passed'] and v['seconds']<=30 and v['scope']['scope_removed'] for v in cases),'six kernel boundaries')
    kernel={}
    for case in ('normal','high','oom','tasks','drift'):
        folder=('kernel-01/' if case=='normal' else 'kernel-02/')+case
        state=read(folder+'/status.json');final=read(folder+'/resources-final.json')
        require(state['attempts']==1 and state['restarts']==0 and state['cleanup_confirmed'] and final['payload_removed'] and
            not final['final']['members'] and final['final']['cgroup.events']['populated']==0,'CPU fault lifecycle')
        kernel[case]=dict(violation=final['violation'],memory_peak_bytes=final['final']['memory.peak'],
            memory_events=final['final']['memory.events'],pids_events=final['final']['pids.events'])
    require(kernel['high']['memory_events']['high']>0 and kernel['high']['violation'] is None,'real high event')
    require(kernel['oom']['violation']=='host_resource_oom' and kernel['oom']['memory_events']['oom_kill']>=2 and
        kernel['oom']['memory_events']['oom_group_kill']>=1,'real group OOM')
    require(kernel['tasks']['violation']=='host_resource_tasks' and kernel['tasks']['pids_events']['max']>0 and
        read('kernel-02/tasks/pids-rejected.json')['errno']==11,'kernel fork refusal')
    require(kernel['drift']['violation']=='host_resource_observation_unavailable','limit drift')
    require(cases[-1]['returncode']==-9 and 'kernel-02/death/resources-final.json' not in data,'controller death evidence')
    require(result['status']=='PASS_HOST_ENVELOPE_SHORT' and result['cleanup_confirmed'] and result['seconds']<=480 and
        result['counts']['generations']==4 and result['counts']['controls']<=64 and not result['long_run'] and
        not result['performance_acceptance'],'bounded business')
    roots=[name.rsplit('/',1)[0] for name in data if name.startswith('owned-generations/generations/') and name.endswith('/reservation.json')]
    require(len(roots)==2,'service count')
    backend=next(p for p in roots if read(p+'/reservation.json')['kind']=='backend')
    front=next(p for p in roots if read(p+'/reservation.json')['kind']=='gateway')
    terminal=read(backend+'/run/terminal.json');final=read(backend+'/run/resources-final.json')
    require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and terminal['host_resource_cleanup']['scope_removed'] and
        terminal['worker']['attempts']==1 and terminal['worker']['restarts']==0 and terminal['guard']['status']=='COMPLETED',
        'live terminal')
    policy=plan['host_resources'];require(policy==final['record']['policy'] and not policy['physical_ram_limit'],'host domain')
    limits=s.resource.limits(policy)
    require(final['payload_removed'] and final['violation'] is None and final['final']['effective_limits']==limits and
        not final['final']['members'] and final['final']['cgroup.events']['populated']==0,'live resource cleanup')
    binding=read('execution/cgroup-binding.json')
    require(binding['record']==final['record'] and binding['record']['owner']==terminal['worker']['owner'],'supervisor binding')
    relative='/'+str(Path(binding['record']['root']).relative_to('/sys/fs/cgroup'))
    require(not binding['controller_membership'].startswith(relative),'lease controller inside payload')
    attachments=[read(name) for name in data if name.startswith(backend+'/run/resource-backend-')]
    require(len(attachments)==1 and attachments[0]['limits']==limits and attachments[0]['membership']==relative+'/payload' and
        attachments[0]['supervisor']==binding['record']['owner'] and attachments[0]['owner']==binding['state']['backend'],
        'pre-exec membership and limits')
    samples=[json.loads(line) for line in data['execution/host-samples.jsonl'].splitlines()]
    capture=read('execution/host-capture.json')
    require(capture['passed'] and capture['samples']==len(samples)>0 and
        capture['bytes']==len(data['execution/host-samples.jsonl']),'host observations')
    require(all(row['effective_limits']==limits and s.resource.violation(row) is None and
        row['memory.current']<=policy['memory_max_bytes'] and row['pids.current']<=policy['tasks_max'] and
        row['memory.swap.current']==0 and all(p['membership']==relative+'/payload' for p in row['processes'])
        for row in samples),'live limits and member inheritance')
    members={p['owner']['pid']:p for row in samples for p in row['processes']}
    log=data[backend+'/run/backend.log'].decode()
    ends=[dict(re.findall(r'([a-z_]+)=([^ ]+)',line)) for line in log.splitlines() if line.startswith('[q4t][request]') and ' event=end ' in line]
    require(len(ends)==4 and all(v['outcome']=='success' and v['reason']=='completed' and v['released']=='1' for v in ends) and
        log.count('grammar worker reaped=1 ')==4 and 'shutdown complete (0 in-flight remaining)' in log,'server completions')
    metrics=read('execution/final-metrics.json');s.a.healthy_idle(metrics,4)
    require(metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4,'grammar metrics')
    gateway_rows=[json.loads(line) for line in data[front+'/run/gateway.log'].splitlines() if line.startswith(b'{')]
    require(len(gateway_rows)==10 and all(sum(v['client']==client and v['status']==200 and v['upstream_status']=='200' and
        v['lane']==lane for v in gateway_rows)==count for client in ('alpha','beta') for lane,count in (('data',4),('control',1))),
        'TLS independent clients')
    usage={};responses=set();ids=set()
    parent=BASE/'offline-verification';parent.mkdir(exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(dir=parent) as temp:
        for client,tokens in (('alpha',512),('beta',64)):
            exported=read('execution/'+client+'-ledger.json');task=exported['meta']['task_id'];ids.add(task)
            prefix='tasks/tasks/'+task+'/'
            folder=Path(temp)/task;folder.mkdir(mode=0o700)
            for name,raw in data.items():
                if name.startswith(prefix):
                    child=name[len(prefix):];require('/' not in child,'nested task record');store.write_new(folder/child,raw)
            require(sha(data[prefix+'ledger.db'])==exported['database_sha256'],'actual task database')
            db=store.connection(folder)
            try:
                ledger=store.Ledger(folder,db,exported['meta']['store_id'])
                require(ledger.events()==exported['events'] and ledger.meta==exported['meta'] and
                    ledger.spec['binding']==plan['bindings'][client] and ledger.spec['max_tokens']==tokens and
                    ledger.spec['runtime_sources']=={n:plan['source_files'][n] for n in ledger.spec['runtime_sources']},'task source/client binding')
                state=engine.public(ledger,engine.replay(ledger))
                value=json.loads(read('execution/'+client+'-completed.json')['stdout'])
                require(dict(state,created=True)==value and state['state']=='COMPLETED' and state['outcome']=='ANSWERED' and
                    state['requests']==state['preflight_checks']==2 and state['saved_tool_results']==state['tool_attempts']==1 and
                    'Qwen4ExpForConditionalGeneration' in state['answer'] and '[docs/MODEL.md:L1-L24]' in state['answer'],
                    'business reconstruction')
                by_kind=lambda kind:[v['payload'] for v in ledger.events() if v['kind']==kind]
                tool=by_kind('tool_started')[0];record=json.loads(by_kind('tool_result')[0]['content'])
                require(tool['name']=='read_project_record' and tool['arguments']==dict(path='docs/MODEL.md',start_line=1,line_count=24) and
                    record['ok'] and record['source']['sha256']==plan['source_files']['docs/MODEL.md'],'real source query')
                usage[client]=[v['completion']['usage'] for v in by_kind('response')]
                require(len(usage[client])==2 and all(0<v['completion_tokens']<=tokens for v in usage[client]),'output budget')
                responses.update(v['completion']['id'] for v in by_kind('response'))
            finally:db.close()
    require(len(ids)==2 and responses=={v['id'] for v in ends},'isolation/request correlation')
    events=[json.loads(line) for line in data[backend+'/run/lease/events.jsonl'].splitlines()]
    observations=[v for v in events if v['event'] in ('preflight','monitor')]
    require(events[0]['event']=='lease_acquired' and events[-1]['event']=='lease_released' and
        events[-1]['status']=='COMPLETED' and all(not v['conflicts'] for v in observations) and
        all(0<=b['monotonic']-a['monotonic']<=10 for a,b in zip(events,events[1:])),'GPU lease')
    seconds=events[-1]['monotonic']-events[0]['monotonic'];require(seconds<=420,'GPU budget')
    post=read('postflight/result.json')
    require(post['passed'] and post['sealed_generations']==2 and post['lock_available'],'postflight')
    return dict(status='ACCEPT_HOST_ENVELOPE_SHORT',cpu_tests=155,corrected_driver_tests=1,stage_driver_tests=2,
        original_high_probe_timeout_preserved=True,kernel_cases=6,kernel=kernel,model_starts=1,gateway_starts=1,
        responses=4,real_tools=2,completed_tasks=2,response_usage=usage,grammar_reaped=4,
        memcg_peak_bytes=final['final']['memory.peak'],memcg_events=final['final']['memory.events'],
        pids_events=final['final']['pids.events'],observed_tasks_max=max(v['pids.current'] for v in samples),
        sampled_processes=len(members),host_samples=len(samples),controls=result['counts']['controls'],
        gpu_seconds=seconds,gpu_observations=len(observations),seconds=result['seconds'],owned_exited=post['owned_exited'],
        physical_ram_limit_proved=False,long_run=False,performance_acceptance=False,cleanup_confirmed=True)


def postflight():
    s.configure();plan=s.check();output=BASE/'postflight';output.mkdir(mode=0o700)
    s.a.require_idle(output/'gpu.json');lock=s.a.guard.common_lock();info=lock.stat()
    require([info.st_dev,info.st_ino]==[66305,3294067] and s.a.lock_available(lock),'original GPU lock')
    result=subprocess.run(['systemctl','--user','show','soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service',
        '-p','LoadState','-p','ActiveState','-p','SubState','-p','MainPID'],capture_output=True,text=True,timeout=10,check=True)
    require(dict(line.split('=',1) for line in result.stdout.splitlines())==
        dict(LoadState='masked',ActiveState='inactive',SubState='dead',MainPID='0'),'SOAR changed')
    (output/'soar-unit.txt').write_text(result.stdout)
    result=subprocess.run(['sudo','-n','docker','ps','--format','{{.Names}}'],capture_output=True,text=True,timeout=10,check=True)
    require(not result.stdout.strip(),'foreign containers; preserve');(output/'containers.txt').write_text(result.stdout)
    with s.retained.store.locked(BASE/'owned-generations') as (path,meta):rows=s.retained.store.inventory(path,meta)
    require(len(rows)==2 and all(v['state']=='SEALED' for v in rows),'generation cleanup')
    owners={}
    for path in (BASE/'owned-generations/generations').iterdir():
        for owner in [pkg.read(path/'reservation.json')['owner'],*pkg.read(path/'seal.json')['proof']['owners']]:
            owners[owner['pid'],owner['start_ticks'],owner['boot_id']]=owner
    for raw in (BASE/'execution/host-samples.jsonl').read_text().splitlines():
        for row in json.loads(raw)['processes']:
            owner=row['owner'];owners[owner['pid'],owner['start_ticks'],owner['boot_id']]=owner
    require(all(not s.retained.r.owner_live(v) for v in owners.values()),'owned process remained')
    pkg.save(output/'result.json',dict(passed=True,owners=list(owners.values()),owned_exited=len(owners),inventory=rows,
        sealed_generations=2,lock_available=True,lock_identity=[info.st_dev,info.st_ino],
        default_unchanged=True,day_plans_unchanged=True,days=plan['days'],new_models_or_http=0))
    return dict(passed=True,owned_exited=len(owners),sealed_generations=2)


def collect():
    paths=[BASE/name for name in ('plan.json','live-command.log','kernel-driver.log','kernel-finish.log')]
    for folder in ('cpu-01','cpu-fix','cpu-driver','kernel-01','kernel-02','execution','owned-generations','tasks','postflight'):
        paths.extend(p for p in (BASE/folder).rglob('*') if p.is_file())
    for folder in ('backend-package','gateway-package','gateway-package/gateway'):
        paths.append(BASE/folder/'manifest.json')
    data={str(p.relative_to(BASE)):p.read_bytes() for p in sorted(set(paths)) if not p.is_symlink()}
    policy=pkg.read(s.previous.BASE/'model-private/policy-private.json')
    secrets=[v['api_key'] for v in policy['clients']]+[pkg.read(s.previous.BASE/'model-private/clients.json')['gateway_token']]
    for name,raw in data.items():
        require(all(secret.encode() not in raw for secret in secrets) and not any(b'-----BEGIN '+k+b'PRIVATE KEY-----' in raw
            for k in (b'',b'RSA ',b'EC ',b'ENCRYPTED ')),'secret in public evidence: '+name)
    local={str(BASE/folder/name):dict(bytes=(BASE/folder/name).stat().st_size,sha256=pkg.sha(BASE/folder/name))
        for folder,name in (('backend-package','q4t'),('gateway-package','nginx'))}
    return data,local


def archive():
    data,local=collect();result=evaluate(data);DEST.mkdir()
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w') as target:
        for name,raw in sorted(data.items()):
            info=tarfile.TarInfo(name);info.size,info.mode,info.mtime=len(raw),0o600,0
            target.addfile(info,io.BytesIO(raw))
    packed=gzip.compress(stream.getvalue(),mtime=0);(DEST/'records.tar.gz').write_bytes(packed)
    pkg.save(DEST/'manifest.json',dict(files={n:dict(bytes=len(raw),sha256=sha(raw)) for n,raw in data.items()},
        local_identities=local,archive=dict(bytes=len(packed),sha256=sha(packed))))
    pkg.save(DEST/'result.json',result)
    return dict(records=len(data),compressed_bytes=len(packed),result=result)


def verify():
    manifest=pkg.read(DEST/'manifest.json');packed=(DEST/'records.tar.gz').read_bytes()
    require(len(packed)==manifest['archive']['bytes'] and sha(packed)==manifest['archive']['sha256'],'archive changed')
    data={}
    with tarfile.open(fileobj=io.BytesIO(packed),mode='r:gz') as source:
        for member in source:
            require(member.isfile() and member.name not in data and member.name in manifest['files'],'archive member')
            raw=source.extractfile(member).read();expected=manifest['files'][member.name]
            require(len(raw)==expected['bytes'] and sha(raw)==expected['sha256'],'archive record changed');data[member.name]=raw
    require(data.keys()==manifest['files'].keys() and evaluate(data)==pkg.read(DEST/'result.json'),'offline result differs')
    return dict(passed=True,records=len(data),new_models_or_http=0)


if __name__=='__main__':
    os.umask(0o077);parser=argparse.ArgumentParser();parser.add_argument('action',choices=('postflight','archive','verify'))
    print(json.dumps(globals()[parser.parse_args().action]()))
