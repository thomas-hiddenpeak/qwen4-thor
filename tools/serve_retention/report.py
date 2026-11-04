"""Audit exact retained-generation records; never launches services or HTTP."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_retention import stage as s

BASE=s.BASE;OUT=s.OUT
DEST=ROOT/'docs/evidence/serve-retention-20261104'
require=s.r.require


def evaluate(data):
    read=lambda name:json.loads(data[name])
    get=lambda name:read('execution/'+name)
    plan=read('plan.json');result=get('result.json')
    require(read('cpu-01/result.json')['passed'] and read('cpu-01/result.json')['tests_run']==20 and
        read('cpu-accepted/result.json')['passed'] and read('cpu-ready/result.json')['passed'] and
        read('cpu-ready/result.json')['effective_tests']==22,'CPU evidence')
    require(not read('cpu-env-original/result.json')['passed'] and
        read('cpu-env-original/result.json')['failures']==1,'original environment classification failure')
    require(result['status']=='PASS_RETAINED_MANAGED_SHORT' and result['cleanup_confirmed'] and
        result['counts']['generations']==6 and result['counts']['negatives']==1 and
        result['counts']['controls']<=64 and result['seconds']<=480,'bounded outcome')
    metrics=get('final-metrics.json');s.a.healthy_idle(metrics,6)
    require(metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==3,
            'grammar cleanup')
    times=[];ids=set();values={}
    for name in ('ordinary','strict','project-read','project-answer','graceful-stream','reconnected'):
        body=get(name+'-request.json');http=get(name+'-http.json')
        value=(s.a.read_sse if body['stream'] else s.a.read_json)(io.BytesIO(data['execution/'+name+'-wire.bin']))
        require(s.gw.asdict(value)==get(name+'-response.json') and value.id==body['request_id'] and
            value.model==body['model'] and value.id not in ids and http['status']==200 and http['eof'],
            'HTTP response identity')
        require(http['client']==('beta' if name=='reconnected' else 'alpha'),'client routing')
        ids.add(value.id);times.append(http['seconds']);values[name]=value
    for name in ('ordinary','reconnected'):
        require(values[name].finish_reason=='stop' and values[name].message['content'].strip()=='710003','ordinary quality')
    require(values['strict'].finish_reason=='stop' and json.loads(values['strict'].message['content'])=={'ok':True},
            'strict quality')
    calls=s.a.prepare_tool_calls(values['project-read'],s.a.VALIDATORS)
    require(len(calls)==1 and calls[0].arguments==dict(path='docs/MODEL.md',start_line=1,line_count=24),'real tool plan')
    receipt=get('project-tool-result.json');tool=json.loads(receipt['content'])
    require(receipt['tool_call_id']==calls[0].call_id and tool['ok'] and
        tool['source']['sha256']==plan['source_files']['docs/MODEL.md'],'real source binding')
    answer=values['project-answer']
    s.a.validate_answer(answer.message['content'],{tool['source']['citation']},1)
    require(answer.finish_reason=='stop' and 'Qwen4ExpForConditionalGeneration' in answer.message['content'],
            'tool answer quality')
    require(values['graceful-stream'].finish_reason=='stop' and
        values['graceful-stream'].message['content'].strip().splitlines()==list(map(str,range(1,41))),
        'active stream not drained')
    require(get('capacity-refusal.json')['returncode']==1 and 'retention capacity exhausted' in
        get('capacity-refusal.json')['stderr'],'capacity was not refused')
    inventory=get('capacity-refusal-inventory.json')
    require(len(inventory)==2 and all(v['state']=='ACTIVE' for v in inventory) and
        sum(v['charged_bytes'] for v in inventory)==128*1024**2,'reserved admission accounting')
    saved=get('saved-generations.json');require(len(saved)==3,'missing sealed generation copy')
    kinds={};owners=[]
    for row in saved:
        prefix='execution/saved-generations/'+row['name']+'/'
        for name,digest in row['files'].items():
            require(hashlib.sha256(data[prefix+name]).hexdigest()==digest,'copied bytes changed')
        reservation=read(prefix+'reservation.json');seal=read(prefix+'seal.json')
        for name,identity in seal['tree']['files'].items():
            raw=data[prefix+name]
            require(len(raw)==identity['bytes'] and hashlib.sha256(raw).hexdigest()==identity['sha256'],'seal identity')
        require(seal['name']==row['name'] and seal['proof']['cleanup_confirmed'],'unproven seal')
        require(read(prefix+'wrapper-terminal.json')['state']=='STOPPED','wrapper failed')
        kind=reservation['kind'];kinds[row['name']]=kind
        owners += [reservation['owner'],*seal['proof']['owners']]
        terminal=read(prefix+'run/terminal.json')
        require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'],'managed cleanup')
        if kind=='backend':
            backend_prefix=prefix
            require(terminal['worker']['attempts']==1 and terminal['worker']['restarts']==0 and
                terminal['guard']['status']=='COMPLETED','backend restarted or guard failed')
        else:
            require(terminal['restarts']==terminal['replayed_requests']==terminal['nginx_returncode']==0,
                    'gateway restarted/replayed')
    require(sorted(kinds.values())==['backend','gateway','gateway'],'generation scope')
    prune=get('prune-plan.json');applied=get('prune-applied.json')
    require(len(prune['remove'])==1 and applied['removed']==[prune['remove'][0]['name']] and
        kinds[applied['removed'][0]]=='gateway' and prune['remove'][0]['state']=='SEALED' and
        any(v['kind']=='backend' and v['state']=='ACTIVE' for v in prune['inventory']),
        'explicit pruning did not protect backend')
    tail=get('tail-receipt.json');raw=data['execution/gateway-tail-private.bin']
    require(0<len(raw)<=65536 and tail['bytes']==len(raw) and hashlib.sha256(raw).hexdigest()==tail['sha256']
        and tail['redacted'] is False,'private tail mismatch')
    events=[json.loads(line) for line in data[backend_prefix+'run/lease/events.jsonl'].splitlines()]
    observations=[v for v in events if v['event'] in ('monitor','preflight')]
    require(events[0]['event']=='lease_acquired' and events[-1]['event']=='lease_released' and
        events[-1]['status']=='COMPLETED' and all(not v['conflicts'] for v in observations) and
        all(0<=b['monotonic']-a['monotonic']<=10 for a,b in zip(events,events[1:])),'guard continuity')
    post=read('postflight/result.json')
    require(post['passed'] and post['day_plans_unchanged'] and post['default_unchanged'] and
        post['lock_available'] and post['sealed_remaining']==2 and post['deleted_gateway_absent'],
        'postflight incomplete')
    return dict(status='ACCEPT_RETAINED_MANAGED_SHORT',cpu_effective_tests=22,model_starts=1,gateway_starts=2,
        successful_requests=6,real_tool_executions=1,grammar_reaped=3,capacity_refusals=1,
        sealed_generations=3,explicitly_pruned_generations=1,owned_exited=post['owned_exited'],
        controls=result['counts']['controls'],seconds=result['seconds'],
        guard_seconds=events[-1]['monotonic']-events[0]['monotonic'],guard_observations=len(observations),
        http_seconds=dict(min=min(times),median=statistics.median(times),max=max(times),scope='request through complete EOF'),
        cleanup_confirmed=True,default_changed=False,long_test_started=False,performance_acceptance=False,
        filesystem_hard_quota_proved=False)


def postflight():
    plan=s.check();out=BASE/'postflight';out.mkdir(mode=0o700)
    s.a.require_idle(out/'gpu.json')
    lock=s.a.guard.common_lock();info=lock.stat()
    require([info.st_dev,info.st_ino]==[66305,3294067] and s.a.lock_available(lock),'original GPU lock not available')
    status=subprocess.run(['systemctl','--user','show','soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service',
        '-p','LoadState','-p','ActiveState','-p','SubState','-p','MainPID'],capture_output=True,text=True,timeout=10,check=True)
    require(dict(line.split('=',1) for line in status.stdout.splitlines())==
        dict(LoadState='masked',ActiveState='inactive',SubState='dead',MainPID='0'),'SOAR changed')
    (out/'soar-unit.txt').write_text(status.stdout)
    containers=subprocess.run(['sudo','-n','docker','ps','--format','{{.Names}}'],capture_output=True,text=True,timeout=10,check=True)
    require(not containers.stdout.strip(),'foreign containers; do not interfere')
    (out/'containers.txt').write_text(containers.stdout)
    unique={}
    for row in s.r.pkg.read(OUT/'saved-generations.json'):
        path=OUT/'saved-generations'/row['name']
        owners=[s.r.pkg.read(path/'reservation.json')['owner'],*s.r.pkg.read(path/'seal.json')['proof']['owners']]
        for owner in owners:unique[owner['pid'],owner['start_ticks'],owner['boot_id']]=owner
    require(all(not s.r.owner_live(v) for v in unique.values()),'owned process remained')
    with store_lock() as values:rows=values
    removed=s.r.pkg.read(OUT/'prune-applied.json')['removed'][0]
    require(len(rows)==2 and all(row['state']=='SEALED' for row in rows) and
        not (s.STORE/'generations'/removed).exists() and not (s.STORE/'gc.json').exists(),'store cleanup state')
    result=dict(passed=True,owned_exited=len(unique),owners=list(unique.values()),inventory=rows,
        sealed_remaining=2,deleted_gateway_absent=True,lock_available=True,lock_identity=[info.st_dev,info.st_ino],
        default_unchanged=True,day_plans_unchanged=True,days=plan['days'],new_models_or_http=0)
    s.r.pkg.save(out/'result.json',result)
    return dict(passed=True,owned_exited=len(unique),day_plans_unchanged=True)


from contextlib import contextmanager
@contextmanager
def store_lock():
    with s.store.locked(s.STORE) as (root,meta):yield s.store.inventory(root,meta)


def collect():
    paths=[BASE/'plan.json',BASE/'gpu-development.json']
    for folder in ('cpu-01','cpu-accepted','cpu-env-original','cpu-ready','execution','postflight'):
        paths += [p for p in (BASE/folder).rglob('*') if p.is_file()]
    data={str(p.relative_to(BASE)):p.read_bytes() for p in sorted(set(paths)) if not p.is_symlink()}
    data['archiver/report.py']=Path(__file__).read_bytes()
    private=s.prior.BASE/'model-private'
    policy=s.r.pkg.read(private/'policy-private.json')
    secrets=[v['api_key'] for v in policy['clients']]+[s.r.pkg.read(private/'clients.json')['gateway_token']]
    for name,raw in data.items():
        require(all(value.encode() not in raw for value in secrets) and not any(
            b'-----BEGIN '+kind+b'PRIVATE KEY-----' in raw for kind in (b'',b'RSA ',b'EC ',b'ENCRYPTED ')),
            'credential material in public records: '+name)
    plan=s.r.pkg.read(BASE/'plan.json');local={}
    for path in (Path(plan['package']['package'])/'q4t',Path(plan['gateways'][0]['package'])/'nginx'):
        local[str(path)]=dict(bytes=path.stat().st_size,sha256=s.r.pkg.sha(path))
    return data,local


def archive():
    data,local=collect();result=evaluate(data);DEST.mkdir()
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w') as target:
        for name,raw in sorted(data.items()):
            info=tarfile.TarInfo(name);info.size,info.mode,info.mtime=len(raw),0o600,0
            target.addfile(info,io.BytesIO(raw))
    packed=gzip.compress(buffer.getvalue(),mtime=0);(DEST/'records.tar.gz').write_bytes(packed)
    s.r.pkg.save(DEST/'manifest.json',dict(files={name:dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        for name,raw in data.items()},local_identities=local,
        archive=dict(bytes=len(packed),sha256=hashlib.sha256(packed).hexdigest())))
    s.r.pkg.save(DEST/'result.json',result)
    return dict(records=len(data),local_identities=len(local),compressed_bytes=len(packed),result=result)


def verify():
    manifest=s.r.pkg.read(DEST/'manifest.json');packed=(DEST/'records.tar.gz').read_bytes()
    require(len(packed)==manifest['archive']['bytes'] and hashlib.sha256(packed).hexdigest()==
        manifest['archive']['sha256'],'archive changed')
    data={}
    with tarfile.open(fileobj=io.BytesIO(packed),mode='r:gz') as source:
        for member in source:
            require(member.isfile() and member.name not in data and member.name in manifest['files'],'unexpected member')
            raw=source.extractfile(member).read();row=manifest['files'][member.name]
            require(len(raw)==row['bytes'] and hashlib.sha256(raw).hexdigest()==row['sha256'],'changed record')
            data[member.name]=raw
    require(data.keys()==manifest['files'].keys() and evaluate(data)==s.r.pkg.read(DEST/'result.json'),'offline outcome differs')
    return dict(passed=True,records=len(data),local_identities=len(manifest['local_identities']),new_model_or_http=0)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('postflight','archive','verify'))
    args=parser.parse_args();print(json.dumps(globals()[args.action]()))
