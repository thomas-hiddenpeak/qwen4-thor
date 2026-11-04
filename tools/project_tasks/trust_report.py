"""Reconstruct trust replacement, unchanged known results and permanent keys."""
import argparse
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
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.project_tasks import trust_stage as s,engine,store,retention as r,migration
from tools.serve_service import package as pkg
from tools.serve_tls import source
from tools.serve_gateway import tls

BASE=s.BASE;DEST=ROOT/'docs/evidence/serve-task-trust-20261104'
require=pkg.require;sha=lambda raw:hashlib.sha256(raw).hexdigest()


def tasks(data,plan,ends):
    read=lambda name:json.loads(data[name]);usage={};ids=set();root_meta=read('tasks/store.json')
    parent=BASE/'offline-verification';parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent) as temp:
        for client,tokens in (('alpha',512),('beta',64)):
            known=read('execution/'+client+'-known-ledger.json');final=read('execution/'+client+'-ledger.json')
            require(known['meta']==final['meta'] and known['events']==final['events'][:len(known['events'])] and
                known['events'][-1]['kind']=='tool_result','saved tool history unchanged')
            prefix='execution/'+client+'-completed-task/';folder=Path(temp)/final['meta']['task_id'];folder.mkdir(mode=0o700)
            for name,raw in data.items():
                if name.startswith(prefix):store.write_new(folder/name[len(prefix):],raw)
            require(sha(data[prefix+'ledger.db'])==final['database_sha256'],'completed ledger identity')
            db=store.connection(folder)
            try:
                ledger=store.Ledger(folder,db,final['meta']['store_id']);state=engine.replay(ledger);public=engine.public(ledger,state)
                require(ledger.meta==final['meta'] and ledger.events()==final['events'] and
                    ledger.spec['runtime_sources']==plan['historical']['sources'] and ledger.spec['binding']==plan['bindings'][client+'-1'] and
                    state['execution_spec']['binding']==plan['bindings'][client+'-2'] and
                    state['execution_spec']['runtime_sources']=={n:plan['source_files'][n] for n in engine.RUNTIME_FILES},'old/new runtime and trust')
                result=json.loads(read('execution/'+client+'-completed.json')['stdout'])
                require(public==result and public['state']=='COMPLETED' and public['outcome']=='ANSWERED' and public['requests']==2 and
                    public['tool_attempts']==public['saved_tool_results']==public['migrations']==1 and
                    'Qwen4ExpForConditionalGeneration' in public['answer'] and '[docs/MODEL.md:L1-L24]' in public['answer'],'actual resumed answer')
                events=ledger.events();by=lambda kind:[v['payload'] for v in events if v['kind']==kind]
                change=by('migration')[0]['plan'];require(change['format']==migration.TRUST_FORMAT and change['source_release']=='srv11f-225e82bd',
                    'explicit accepted old source migration')
                migration.same_trust_identity(change['source_spec']['binding'],change['target_spec']['binding'])
                require(by('tool_started')[0]['name']=='read_project_record' and by('tool_started')[0]['arguments']==
                    dict(path='docs/MODEL.md',start_line=1,line_count=24),'real requested tool')
                actual=json.loads(by('tool_result')[0]['content']);require(actual['ok'] and actual['source']['sha256']==plan['source_files']['docs/MODEL.md'],'actual source')
                read_observations=[json.loads(v) for v in data['execution/'+client+'-old/read-observations.jsonl'].splitlines()]
                require(len(read_observations)==1 and read_observations[0]['invocation']['name']=='read_project_record','exactly one actual tool read')
                usage[client]=[v['completion']['usage'] for v in by('response')]
                require(len(usage[client])==2 and all(0<v['completion_tokens']<=tokens for v in usage[client]),'client output budgets')
                ids.update(v['completion']['id'] for v in by('response'))
                require(json.loads(read('execution/'+client+'-duplicate-apply.json')['stdout'])['already_applied'] and
                    json.loads(read('execution/'+client+'-duplicate-submit.json')['stdout'])==dict(public,created=False),'active duplicate no execution')
                archived=read('execution/'+client+'-archive-key.json');name=ledger.meta['task_id'];archived_state=r.key_state(archived,root_meta,name)
                require(archived_state['binding']==plan['bindings'][client+'-1'] and archived_state['rekeys']==1 and
                    archived['events'][-1]['kind']=='trust' and archived_state['payload']==('retained' if client=='alpha' else 'purged') and
                    archived['base']['contract_sha256']==r.business_contract(state['execution_spec']) and
                    archived['base']['answer_sha256']==store.digest(public['answer']),'permanent key trust-only history')
                original=dict(state['execution_spec'],binding=plan['bindings'][client+'-1']);normalized=json.loads(json.dumps(original))
                normalized['binding']['ca_sha256']=archived['base']['binding']['ca_sha256']
                require(r.business_contract(normalized)==archived['base']['contract_sha256'],'original archive business contract retained')
                for label in ('archive-duplicate-submit','archive-resume'):
                    value=json.loads(read('execution/'+client+'-'+label+'.json')['stdout'])
                    require(value['state']=='ARCHIVED' and value['no_execution'] and value['payload_state']==archived_state['payload'],'archived duplicate')
                require(json.loads(read('execution/'+client+'-archive-duplicate-apply.json')['stdout'])['already_applied'],'duplicate archive migration')
                if client=='alpha':
                    require(all(sha(data['tasks/retention/payloads/'+name+'/'+n])==v['sha256'] for n,v in archived_state['files'].items()),'retained original bytes')
                else:require(not any(n.startswith('tasks/retention/payloads/'+name+'/') for n in data),'purged payload remains')
            finally:db.close()
    require(ids=={v['id'] for v in ends},'actual backend request identity correlation')
    return usage


def evaluate(data):
    read=lambda name:json.loads(data[name]);plan=read('plan.json');result=read('execution/result.json')
    for folder,count in (('cpu-01',124),('cpu-driver',4)):
        v=read(folder+'/result.json');require(v['passed'] and v['tests_run']==count and v['errors']==v['failures']==0 and
            v['models_started']==v['listeners_started']==v['gpu_started']==0 and sha(data[folder+'/result.json'])==plan['receipts'][folder],'CPU gate')
        for name,digest in v['sources'].items():require(sha(data[folder+'/source/'+name])==digest==plan['source_files'][name],'CPU source identity')
    for name,digest in plan['historical']['sources'].items():require(sha(data['historical-source/'+name])==digest,'old source identity')
    require(result['status']=='PASS_TASK_TRUST_SHORT' and result['cleanup_confirmed'] and result['seconds']<=240 and result['counts']['controls']<=64 and
        result['counts']['generations']==4 and result['tls_probes']==2 and result['negative_tls']==1 and not result['long_run'],'real bounded outcome')
    fingerprints=[]
    for index in (1,2):
        outer=read(f'gateway-{index}/manifest.json');inner=read(f'gateway-{index}/gateway/manifest.json')
        require(sha(data[f'gateway-{index}/manifest.json'])==plan['gateways'][index-1]['manifest_sha256'] and outer['version']==4 and inner['version']==3 and
            sha(data[f'gateway-{index}/gateway/manifest.json'])==outer['gateway']['manifest_sha256'],'gateway identity')
        observed=read(f'execution/gateway-{index}-tls.json');cert=data[f'gateway-{index}/gateway/certificate.pem'];ca=data[f'gateway-{index}/gateway/trusted-ca.pem']
        fingerprints.append(tls.certificates(cert)[0][1]);require(observed['leaf_sha256']==fingerprints[-1] and
            observed['ca_sha256']==sha(ca)==plan['authorities'][index-1]['sha256'] and not observed['rejected'] and observed['http_requests_sent']==0,'actual verified TLS')
        terminal=read(f'execution/gateway-{index}-run/terminal.json')
        require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and terminal['starts']==1 and terminal['restarts']==0 and
            not terminal['backend_stopped_by_gateway'] and not terminal['automatic_reactivation'],'gateway cleanup')
    negative=read('execution/old-ca-rejected.json');require(negative['rejected'] and negative['verify_code'] is not None and
        negative['http_requests_sent']==0 and negative['ca_sha256']==plan['authorities'][0]['sha256'],'old CA rejected before HTTP')
    backend='execution/managed-run';terminal=read(backend+'/terminal.json');final=read(backend+'/resources-final.json')
    require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and terminal['worker']['attempts']==1 and terminal['worker']['restarts']==0 and
        terminal['host_resource_cleanup']['scope_removed'] and terminal['guard']['status']=='COMPLETED' and final['payload_removed'] and
        final['violation'] is None and not final['final']['members'],'backend resource cleanup')
    handoff=read('execution/handoff.json');require(handoff['before_backend']==handoff['after_backend']==read(backend+'/attempt-1.json')['owner'],'same original backend')
    for client in ('alpha','beta'):
        require(read('execution/'+client+'-old/killed.json')['returncode']==-9,'actual application interruption')
        for label,code in (('unmigrated','binding_changed'),('wrong-operation','plan_changed')):
            value=read('execution/'+client+'-'+label+'.json');require(value['returncode']==1 and json.loads(value['stderr'])['code']==code,'metadata refusal')
    log=data[backend+'/backend.log'].decode();ends=[dict(re.findall(r'([a-z_]+)=([^ ]+)',v)) for v in log.splitlines() if v.startswith('[q4t][request]') and ' event=end ' in v]
    require(len(ends)==4 and all(v['outcome']=='success' and v['reason']=='completed' and v['released']=='1' for v in ends) and
        log.count('grammar worker reaped=1 ')==4 and 'shutdown complete (0 in-flight remaining)' in log,'four actual complete responses/reaps')
    s.a.healthy_idle(read('execution/final-metrics.json'),4);usage=tasks(data,plan,ends)
    retained=read('execution/retention-status.json');require(retained['active_tasks']==0 and retained['archived_keys']==2 and retained['retained_payloads']==1,'final permanent keys')
    events=[json.loads(v) for v in data[backend+'/lease/events.jsonl'].splitlines()];observations=[v for v in events if v['event'] in ('preflight','monitor')]
    require(events[0]['event']=='lease_acquired' and events[-1]['event']=='lease_released' and events[-1]['status']=='COMPLETED' and observations and
        all(v['conflicts']==[] for v in observations) and all(0<=b['monotonic']-a['monotonic']<=10 for a,b in zip(events,events[1:])),'continuous GPU lease')
    seconds=events[-1]['monotonic']-events[0]['monotonic'];require(seconds<=210 and
        read('execution/guard-archive.json')==dict(passed=True,records=len(events),observations=len(observations),seconds=seconds),'guard reconstruction')
    require(b'owner_identity_invalid' in data['postflight-command.log'] and b'task identity changed' in data['archive-command.log'],
        'original offline process/task identity extraction failures preserved')
    post=read('postflight-corrected/result.json');require(post['passed'] and post['lock_available'] and post['ports_available'] and post['lock_identity']==[66305,3294067] and
        post['days']==plan['days'] and post['default_unchanged'] and read('postflight-corrected/gpu.json')['pids']==[] and not data['postflight-corrected/containers.txt'].strip(),'postflight')
    require(dict(v.split('=',1) for v in data['postflight-corrected/soar-unit.txt'].decode().splitlines())==
        dict(LoadState='masked',ActiveState='inactive',SubState='dead',MainPID='0'),'vLLM recovery stayed off')
    return dict(passed=True,cpu_tests=124,driver_tests=4,cpu_file_operation_kills=len(read('cpu-01/result.json')['file_operation_kills']),
        models=1,gateways=2,application_kills=2,complete_responses=4,real_tools=2,completed_tasks=2,response_usage=usage,
        verified_tls=2,old_ca_rejected_before_http=1,active_ca_migrations=2,archived_ca_migrations=2,duplicate_applies=4,duplicate_submits=4,
        archived_resumes_without_execution=2,retained_payloads=1,permanent_keys=2,metadata_refusals=4,gpu_seconds=seconds,
        gpu_observations=len(observations),seconds=result['seconds'],controls=result['counts']['controls'],owned_exited=post['owned_exited'],
        memcg_peak_bytes=final['final']['memory.peak'],memcg_events=final['final']['memory.events'],pids_events=final['final']['pids.events'],
        cleanup_confirmed=True,long_run=False,actual_lan_accepted=False,physical_ram_limit=False,performance_acceptance=False,
        original_offline_owner_extraction_error_preserved=True,original_offline_task_directory_error_preserved=True)


def frozen_runtime_plan():
    """Runtime stays frozen; only this separately pinned offline report changed."""
    plan=pkg.read(s.PLAN);report='tools/project_tasks/trust_report.py'
    for name,digest in plan['source_files'].items():
        path=BASE/'cpu-driver/source'/name if name==report else ROOT/name
        require(pkg.sha(path)==digest,'frozen runtime source changed: '+name)
    for name,digest in {**plan['default'],**plan['private'],**plan['days']}.items():require(pkg.sha(ROOT/name)==digest,'fixed artifact changed')
    for name in plan['days']:require(not (Path(name).parent/'execution').exists(),'user day executed')
    for folder,digest in plan['receipts'].items():require(pkg.sha(BASE/folder/'result.json')==digest,'CPU receipt changed')
    require(pkg.sha(plan['reused_plan']['path'])==plan['reused_plan']['sha256'] and
        pkg.sha(ROOT/plan['historical_archive'])==plan['historical_archive_sha256'],'historical evidence changed')
    for row in plan['original_packages']:require(pkg.sha(Path(row['package'])/'manifest.json')==row['manifest_sha256'],'old package changed')
    pkg.verify(plan['package']['package'],plan['package']['manifest_sha256'])
    for row in plan['gateways']:s.gateway.verify(row['package'],row['manifest_sha256'])
    for row in plan['authorities']:require(pkg.sha(row['path'])==row['sha256'],'CA changed')
    for key,row in plan['profiles'].items():require(pkg.sha(row['path'])==row['sha256'] and s.transport.profile(row['path'])[1]==plan['bindings'][key],'profile changed')
    for name,digest in plan['historical']['sources'].items():require(pkg.sha(s.HISTORICAL/name)==digest,'historical engine changed')
    return plan


def postflight():
    s.configure();plan=frozen_runtime_plan();output=BASE/'postflight-corrected';output.mkdir(mode=0o700);s.a.require_idle(output/'gpu.json')
    lock=s.a.guard.common_lock();info=lock.stat();require([info.st_dev,info.st_ino]==[66305,3294067] and s.a.lock_available(lock),'original lock')
    command=subprocess.run(['systemctl','--user','show','soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service',
        '-p','LoadState','-p','ActiveState','-p','SubState','-p','MainPID'],capture_output=True,text=True,timeout=10,check=True)
    require(dict(v.split('=',1) for v in command.stdout.splitlines())==dict(LoadState='masked',ActiveState='inactive',SubState='dead',MainPID='0'),'SOAR changed')
    (output/'soar-unit.txt').write_text(command.stdout)
    command=subprocess.run(['sudo','-n','docker','ps','-a','--format','{{.Names}}'],capture_output=True,text=True,timeout=10,check=True)
    require(not command.stdout.strip(),'foreign containers; preserve');(output/'containers.txt').write_text(command.stdout)
    owners={}
    def visit(value):
        if isinstance(value,dict):
            if {'pid','start_ticks','boot_id'}<=value.keys():
                owners[value['pid'],value['start_ticks'],value['boot_id']]={key:value[key] for key in ('pid','start_ticks','boot_id')}
            else:
                for v in value.values():visit(v)
        elif isinstance(value,list):
            for v in value:visit(v)
    for path in (BASE/'execution').rglob('*.json'):
        if path.stat().st_size<=2*1024**2:visit(pkg.read(path))
    require(owners and all(not source.live(v) and not s.sup.processes.group_members(v['pid']) for v in owners.values()),'owned process remained')
    for port in (18128,18448):
        with socket.socket() as sock:sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind(('127.0.0.1',port))
    release=pkg.read(ROOT/'build/q4t.release.json');runtime=pkg.read(release['tested_source_manifest'])
    require(all(pkg.sha(ROOT/n)==digest for n,digest in runtime.items()),'runtime source changed')
    pkg.save(output/'result.json',dict(passed=True,owners=list(owners.values()),owned_exited=len(owners),lock_available=True,
        lock_identity=[info.st_dev,info.st_ino],ports_available=True,default_unchanged=True,days=plan['days'],runtime_sources=runtime,new_models_or_http=0))
    return dict(passed=True,owned_exited=len(owners))


def collect():
    paths=[BASE/n for n in ('plan.json','live-command.log','postflight-command.log','postflight-corrected-command.log','archive-command.log')]
    for folder in ('cpu-01','cpu-driver','execution','plans','postflight','postflight-corrected'):paths.extend(p for p in (BASE/folder).rglob('*') if p.is_file())
    for index in (1,2):paths.extend(BASE/f'gateway-{index}'/n for n in ('manifest.json','gateway/manifest.json','gateway/certificate.pem','gateway/trusted-ca.pem'))
    data={str(p.relative_to(BASE)):p.read_bytes() for p in sorted(set(paths)) if p.exists() and not p.is_symlink()}
    plan=pkg.read(BASE/'plan.json')
    for name in plan['historical']['sources']:data['historical-source/'+name]=(s.HISTORICAL/name).read_bytes()
    for path in s.TASKS.rglob('*'):
        if path.is_file():data['tasks/'+str(path.relative_to(s.TASKS))]=path.read_bytes()
    data['reconstruction/tools/project_tasks/trust_report.py']=Path(__file__).read_bytes()
    private=ROOT/'.q4t-work/serve-clients-managed-20261104/model-private'
    secrets=[v['api_key'] for v in pkg.read(private/'policy-private.json')['clients']]+[pkg.read(private/'clients.json')['gateway_token']]
    for name,raw in data.items():require(all(v.encode() not in raw for v in secrets) and not any(b'-----BEGIN '+k+b'PRIVATE KEY-----' in raw
        for k in (b'',b'RSA ',b'EC ',b'ENCRYPTED ')),'secret in evidence: '+name)
    binaries=[Path(plan['package']['package'])/'q4t',BASE/'gateway-1/nginx',BASE/'gateway-2/nginx',tls.OPENSSL,Path(sys.executable).resolve()]
    return data,{str(p):dict(bytes=p.stat().st_size,sha256=pkg.sha(p)) for p in binaries}


def archive():
    data,local=collect();result=evaluate(data);DEST.mkdir();stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w') as target:
        for name,raw in sorted(data.items()):
            info=tarfile.TarInfo(name);info.size,info.mode,info.mtime=len(raw),0o600,0;target.addfile(info,io.BytesIO(raw))
    packed=gzip.compress(stream.getvalue(),mtime=0);(DEST/'records.tar.gz').write_bytes(packed)
    pkg.save(DEST/'manifest.json',dict(files={n:dict(bytes=len(raw),sha256=sha(raw)) for n,raw in data.items()},local_identities=local,
        archive=dict(bytes=len(packed),sha256=sha(packed)),reconstruction={n:pkg.sha(ROOT/n) for n in
            ('tools/project_tasks/trust_report.py',*engine.RUNTIME_FILES)}))
    pkg.save(DEST/'result.json',result);return dict(records=len(data),compressed_bytes=len(packed),result=result)


def verify():
    manifest=pkg.read(DEST/'manifest.json');packed=(DEST/'records.tar.gz').read_bytes()
    require(all(pkg.sha(ROOT/n)==digest for n,digest in manifest['reconstruction'].items()),'use pinned reconstruction and application sources')
    require(len(packed)==manifest['archive']['bytes'] and sha(packed)==manifest['archive']['sha256'],'archive identity');data={}
    with tarfile.open(fileobj=io.BytesIO(packed),mode='r:gz') as source_:
        for member in source_:
            require(member.isfile() and member.name not in data and member.name in manifest['files'],'archive member')
            raw=source_.extractfile(member).read();expected=manifest['files'][member.name]
            require(len(raw)==expected['bytes'] and sha(raw)==expected['sha256'],'record identity');data[member.name]=raw
    require(data.keys()==manifest['files'].keys() and evaluate(data)==pkg.read(DEST/'result.json'),'offline result differs')
    return dict(passed=True,records=len(data),new_models_or_http=0)


if __name__=='__main__':
    os.umask(0o077);parser=argparse.ArgumentParser();parser.add_argument('action',choices=('postflight','archive','verify'))
    print(json.dumps(globals()[parser.parse_args().action]()))
