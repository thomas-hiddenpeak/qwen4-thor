"""Read-only managed-client acceptance audit and public short evidence archive."""
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
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_clients import managed_stage as s

BASE=s.BASE
DEST=ROOT/'docs/evidence/serve-clients-managed-20261104'
require=s.pkg.require


def evaluate(raw,profile='short'):
    read=lambda name:json.loads(raw(name))
    prefix=profile+'/execution/'
    get=lambda name:read(prefix+name)
    plan=read(profile+'/plan.json');config=s.PROFILES[profile]
    cpu=read('cpu-accepted/result.json')
    require(cpu['passed'] and cpu['effective_tests']==38,'CPU acceptance')
    require(not read('cpu/result.json')['passed'] and read('cpu/result.json')['errors']==2 and
            not read('cpu-corrected/result.json')['passed'] and
            read('cpu-corrected/result.json')['errors']==1,'original CPU failures missing')
    require(read('original-rejection.json')['status']=='REPRODUCED_RELEASE_RECEIPT_MISMATCH',
            'original real packaging rejection missing')
    result=get('result.json');expected=config['cycles']*5+2
    require(result['status']=='PASS_MANAGED_CLIENT_'+profile.upper() and result['cleanup_confirmed'] and
            result['successful_requests']==expected and result['real_tool_executions']==config['cycles'] and
            result['model_starts']==1 and result['gateway_starts']==2,'managed outcome')
    require(result['counts']['generations']==expected and result['counts']['negatives']==3 and
            result['counts']['controls']<=config['controls'] and
            result['elapsed_seconds']<=config['total_max'],'bounded attempt')
    require(result['long_run']==result['day_scale_stability_proved']==(profile=='day') and
            result['performance_acceptance'] is False,'duration claim')
    final=get('final-metrics.json');s.a.healthy_idle(final,expected)
    require(final['q4t_structured_workers_started_total']==
            final['q4t_structured_workers_reaped_total']==config['cycles']*3,'grammar cleanup')
    ids=set();seconds=[];clients={'alpha':0,'beta':0};resources=[]

    def completion(label,client):
        body=get(label+'-request.json');http=get(label+'-http.json')
        value=(s.a.read_sse if body['stream'] else s.a.read_json)(io.BytesIO(raw(prefix+label+'-wire.bin')))
        require(s.gw.asdict(value)==get(label+'-response.json') and value.id==body['request_id'] and
                value.model==body['model'] and value.id not in ids,'response identity/parser mismatch')
        require(http['status']==200 and http['eof'] and http['client']==client,'TLS client result')
        ids.add(value.id);seconds.append(http['seconds']);clients[client]+=1
        return value

    for n in range(1,config['cycles']+1):
        label=f'cycle-{n:04d}/'
        for name,client in [('plain','alpha'),('stream','beta')]:
            value=completion(label+name,client)
            require(value.finish_reason=='stop' and value.message['content'].strip()=='710003','ordinary answer')
        value=completion(label+'strict','alpha')
        require(value.finish_reason=='stop' and json.loads(value.message['content'])=={'ok':True},'strict answer')
        planned=s.a.prepare_tool_calls(completion(label+'project-read','alpha'),s.a.VALIDATORS)
        require(len(planned)==1 and planned[0].arguments==
                dict(path='docs/MODEL.md',start_line=1,line_count=24),'real tool arguments')
        tool=get(label+'project-tool-result.json');record=json.loads(tool['content'])
        require(tool['tool_call_id']==planned[0].call_id and record['ok'] and
                record['source']['sha256']==plan['source_files']['docs/MODEL.md'],'real tool source')
        answer=completion(label+'project-answer','alpha')
        s.a.validate_answer(answer.message['content'],{record['source']['citation']},1)
        require(answer.finish_reason=='stop' and 'Qwen4ExpForConditionalGeneration' in
                answer.message['content'],'cited project answer')
        preflight=get(label+'preflight-response.json')
        require(preflight['status']==200 and preflight['body']['output']['requested_tokens']==64 and
                preflight['body']['client_limits']['max_output_tokens']==64,'beta effective budget')
        resources.append(s.evidence.values(get(label+'quiescent.json')))
    drained=completion('graceful-stream','alpha')
    require(drained.finish_reason=='stop' and drained.message['content'].strip().splitlines()==
            list(map(str,range(1,41))),'active stream not fully drained')
    resumed=completion('reconnected-beta','beta')
    require(resumed.finish_reason=='stop' and resumed.message['content'].strip()=='710003','reconnected response')
    require(len(ids)==expected,'request count')
    for n in (0,1):
        terminal=get(f'rejected-front-{n}/terminal.json')
        require(terminal['state']=='FAILED' and terminal['cleanup_confirmed'] and
                'client policy differs' in terminal['error'],'binding rejection')
    refusal=get('beta-over-budget-response.json')
    require(refusal['status']==400 and refusal['body']['error']['code']==
            'client_output_limit_exceeded','budget rejection')
    backend=get('managed-run/terminal.json');exit_record=get('managed-run/lease/exit.json')
    require(backend['state']=='STOPPED' and backend['cleanup_confirmed'] and
            backend['worker']['attempts']==1 and backend['worker']['restarts']==0 and
            backend['guard']==exit_record and exit_record['status']=='COMPLETED','managed terminal')
    events=[json.loads(line) for line in raw(prefix+'capture/events.jsonl').splitlines()]
    starts=[v for v in events if v['event']=='lease_acquired']
    ends=[v for v in events if v['event']=='lease_released']
    observations=[v for v in events if v['event'] in ('monitor','preflight')]
    require(len(starts)==len(ends)==1 and ends[0]['status']=='COMPLETED' and
            all(not row['conflicts'] for row in observations),'guard ownership')
    require(events[0]==starts[0] and events[-1]==ends[0] and all(
        0<=later['monotonic']-earlier['monotonic']<=10 for earlier,later in zip(events,events[1:])),
        'guard timeline incomplete')
    for n in (1,2):
        terminal=get(f'front-{n}/terminal.json');gone=get(f'front-{n}-gone.json')
        require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and
                terminal['restarts']==terminal['replayed_requests']==terminal['nginx_returncode']==0 and
                gone['passed'] and gone['claim_available'] and gone['port_available'],'front cleanup')
    require(get('front-2/terminal.json')['finished_unix']<=backend['finished_unix'],
            'frontend outlived backend lease')
    require('LoadState=not-found' in get('unit-removed.json')['stdout'],'temporary unit remains')
    capture=get('capture/capture.json')
    require(capture['passed'] and capture['resource_samples']>0,'continuous evidence')
    for name,row in capture['logs'].items():
        payload=raw(prefix+'capture/'+name)
        require(row['bytes']==len(payload) and row['sha256']==hashlib.sha256(payload).hexdigest() and
                row['complete_observed_chain'],'capture copy identity')
    soak=get('soak.json')
    require(soak['passed'] and soak['cycles']==config['cycles'] and
            config['seconds']<=soak['seconds']<=config['soak_max'],'duration coverage')
    if profile=='day':
        hour=get('first-hour.json')
        require(hour==s.soak.hourly_checkpoint(240,hour['seconds'],resources[:240]) and
                soak['day_scale_stability_proved'] and
                soak['envelope']==s.evidence.envelope(resources),'hour/day resource envelope')
    return dict(status='ACCEPT_MANAGED_CLIENT_'+profile.upper(),cpu_effective_tests=38,
        model_starts=1,gateway_starts=2,successful_requests=expected,real_tool_executions=config['cycles'],
        client_successes=clients,grammar_reaped=config['cycles']*3,controls=result['counts']['controls'],
        guard_seconds=ends[0]['monotonic']-starts[0]['monotonic'],guard_observations=len(observations),
        elapsed_seconds=result['elapsed_seconds'],soak_seconds=soak['seconds'],
        resource_samples=capture['resource_samples'],http_seconds=dict(
            min=min(seconds),median=statistics.median(seconds),max=max(seconds),
            scope='connection request through complete EOF; bounded business, not performance acceptance'),
        cleanup_confirmed=True,performance_acceptance=False,day_scale_stability_proved=profile=='day')


def postflight():
    s.configure('short');plan=s.check()
    output=BASE/'postflight';output.mkdir(mode=0o700)
    s.a.require_idle(output/'gpu.json')
    lock=s.a.guard.common_lock();stat=lock.stat()
    require([stat.st_dev,stat.st_ino]==[66305,3294067] and s.a.lock_available(lock),'GPU lock retained/replaced')
    unit='soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'
    status=subprocess.run(['systemctl','--user','show',unit,'-p','LoadState','-p','ActiveState',
        '-p','SubState','-p','MainPID'],capture_output=True,text=True,timeout=10,check=True)
    (output/'soar-unit.txt').write_text(status.stdout)
    fields=dict(line.split('=',1) for line in status.stdout.splitlines())
    require(fields==dict(LoadState='masked',ActiveState='inactive',SubState='dead',MainPID='0'),'SOAR state changed')
    containers=subprocess.run(['sudo','-n','docker','ps','--format','{{.Names}}'],
        capture_output=True,text=True,timeout=10,check=True)
    require(not containers.stdout.strip(),'unexpected containers; do not intervene')
    (output/'containers.txt').write_text(containers.stdout)
    rows=[]
    owners=s.pkg.read(s.OUT/'ready-ownership.json')['owners']
    for n in (1,2):
        front=s.pkg.read(s.OUT/f'front-{n}-ready.json')
        owners += [front['control']['owner'],front['status']['observation']['nginx']]
    for n in (0,1):owners.append(s.pkg.read(s.OUT/f'rejected-front-{n}/controller.json')['owner'])
    owners.append(s.pkg.read(s.OUT/'driver.json'))
    for owner in owners:
        require(not s.m.alive(owner),'owned process still alive')
        rows.append(dict(owner=owner,exited=True))
    s.configure('day');day=s.check()
    require(not s.OUT.exists(),'operator day unexpectedly executed')
    result=dict(passed=True,owners=rows,lock_identity=[stat.st_dev,stat.st_ino],lock_available=True,
        default_unchanged=True,old_day_plans_unchanged=True,day_prepared_not_run=True,
        day_sha256=s.pkg.sha(s.PLAN),short_sha256=s.pkg.sha(BASE/'short/plan.json'),new_model_or_http=0)
    s.pkg.save(output/'result.json',result)
    return dict(passed=True,owned_exited=len(rows),day_prepared_not_run=True)


def collect():
    paths=[BASE/'original-package.py',BASE/'original-rejection.json',BASE/'preparation-gpu.json',
           BASE/'bundle/receipt.json',BASE/'short/plan.json',BASE/'day/plan.json']
    for name in ('cpu','cpu-corrected','cpu-accepted','short/execution','postflight'):
        paths += [p for p in (BASE/name).rglob('*') if p.is_file()]
    local={}
    for name in ('backend-package','gateway-package','gateway-mismatch-package','gateway-unscoped-package'):
        folder=BASE/name;paths.append(folder/'manifest.json')
        if name=='backend-package':
            paths += [folder/n for n in ('q4t.release.json','acceptance.json','CMakeCache.txt')]
            binary=folder/'q4t'
        else:
            paths.append(folder/'gateway/manifest.json');binary=folder/'nginx'
        require(binary.is_file(),'packaged executable missing')
        local[str(binary)]=dict(bytes=binary.stat().st_size,sha256=s.pkg.sha(binary))
    data={str(path.relative_to(BASE)):path.read_bytes() for path in sorted(set(paths))
          if not path.is_symlink()}
    data['archiver/managed_report.py']=Path(__file__).read_bytes()
    policy=s.pkg.read(BASE/'model-private/policy-private.json')
    secrets=[v['api_key'] for v in policy['clients']]
    secrets += [s.pkg.read(BASE/'model-private'/n)['gateway_token']
                for n in ('clients.json','wrong-clients.json')]
    for name,value in data.items():
        require(all(token.encode() not in value for token in secrets) and
                not any(b'-----BEGIN '+t+b'PRIVATE KEY-----' in value for t in
                        (b'',b'RSA ',b'EC ',b'ENCRYPTED ')),'private material in public evidence: '+name)
    return data,local


def archive():
    data,local=collect();result=evaluate(data.__getitem__)
    require(json.loads(data['postflight/result.json'])['passed'],'postflight missing')
    DEST.mkdir()
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w') as target:
        for name,value in sorted(data.items()):
            entry=tarfile.TarInfo(name);entry.size,entry.mode,entry.mtime=len(value),0o600,0
            target.addfile(entry,io.BytesIO(value))
    packed=gzip.compress(buffer.getvalue(),mtime=0)
    (DEST/'records.tar.gz').write_bytes(packed)
    s.pkg.save(DEST/'manifest.json',dict(files={name:dict(bytes=len(value),sha256=hashlib.sha256(value).hexdigest())
        for name,value in sorted(data.items())},local_identities=local,
        archive=dict(bytes=len(packed),sha256=hashlib.sha256(packed).hexdigest())))
    s.pkg.save(DEST/'result.json',result)
    return dict(records=len(data),local_identities=len(local),compressed_bytes=len(packed),result=result)


def verify():
    manifest=s.pkg.read(DEST/'manifest.json');packed=(DEST/'records.tar.gz').read_bytes()
    require(len(packed)==manifest['archive']['bytes'] and hashlib.sha256(packed).hexdigest()==
            manifest['archive']['sha256'],'archive identity')
    data={}
    with tarfile.open(fileobj=io.BytesIO(packed),mode='r:gz') as archive:
        for member in archive:
            require(member.isfile() and member.name in manifest['files'] and member.name not in data,'archive member')
            raw=archive.extractfile(member).read();row=manifest['files'][member.name]
            require(len(raw)==row['bytes'] and hashlib.sha256(raw).hexdigest()==row['sha256'],'record identity')
            data[member.name]=raw
    require(data.keys()==manifest['files'].keys() and evaluate(data.__getitem__)==
            s.pkg.read(DEST/'result.json'),'offline outcome')
    return dict(passed=True,records=len(data),local_identities=len(manifest['local_identities']),new_model_or_http=0)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('check','postflight','archive','verify'))
    parser.add_argument('--profile',choices=tuple(s.PROFILES),default='short')
    args=parser.parse_args()
    if args.action=='check':value=evaluate(lambda name:(BASE/name).read_bytes(),args.profile)
    else:value=globals()[args.action]()
    print(json.dumps(value,ensure_ascii=False))
