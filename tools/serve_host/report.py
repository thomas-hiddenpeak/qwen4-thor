"""Archive and independently reconstruct host observation acceptance, offline."""
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
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_host import stage as s
from tools.serve_service import package as pkg
from tools.project_tasks import engine,store

BASE=s.BASE
DEST=ROOT/'docs/evidence/serve-host-observation-20261104'
require=pkg.require
sha=lambda raw:hashlib.sha256(raw).hexdigest()


def tasks(data,plan,ends):
    read=lambda name:json.loads(data[name]);usage={};responses=set();ids=set()
    parent=BASE/'offline-verification';parent.mkdir(exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(dir=parent) as temp:
        for client,tokens in (('alpha',512),('beta',64)):
            exported=read('execution/'+client+'-ledger.json');task=exported['meta']['task_id'];ids.add(task)
            prefix='tasks/tasks/'+task+'/';folder=Path(temp)/task;folder.mkdir(mode=0o700)
            for name,raw in data.items():
                if name.startswith(prefix):
                    child=name[len(prefix):];require('/' not in child,'nested task record');store.write_new(folder/child,raw)
            require(sha(data[prefix+'ledger.db'])==exported['database_sha256'],'task database identity')
            db=store.connection(folder)
            try:
                ledger=store.Ledger(folder,db,exported['meta']['store_id'])
                require(ledger.events()==exported['events'] and ledger.meta==exported['meta'] and
                    ledger.spec['binding']==plan['bindings'][client] and ledger.spec['max_tokens']==tokens and
                    ledger.spec['runtime_sources']=={n:plan['source_files'][n] for n in ledger.spec['runtime_sources']},
                    'task source/client binding')
                state=engine.public(ledger,engine.replay(ledger))
                value=json.loads(read('execution/'+client+'-completed.json')['stdout'])
                require(dict(state,created=True)==value and state['state']=='COMPLETED' and state['outcome']=='ANSWERED' and
                    state['requests']==state['preflight_checks']==2 and state['saved_tool_results']==state['tool_attempts']==1 and
                    'Qwen4ExpForConditionalGeneration' in state['answer'] and '[docs/MODEL.md:L1-L24]' in state['answer'],
                    'real business result')
                by_kind=lambda kind:[v['payload'] for v in ledger.events() if v['kind']==kind]
                tool=by_kind('tool_started')[0];record=json.loads(by_kind('tool_result')[0]['content'])
                require(tool['name']=='read_project_record' and tool['arguments']==dict(path='docs/MODEL.md',start_line=1,line_count=24) and
                    record['ok'] and record['source']['sha256']==plan['source_files']['docs/MODEL.md'],'real project tool')
                usage[client]=[v['completion']['usage'] for v in by_kind('response')]
                require(len(usage[client])==2 and all(0<v['completion_tokens']<=tokens for v in usage[client]),'output budget')
                responses.update(v['completion']['id'] for v in by_kind('response'))
            finally:db.close()
    require(len(ids)==2 and responses=={v['id'] for v in ends},'request correlation')
    return usage


def evaluate(data):
    read=lambda name:json.loads(data[name])
    plan=read('plan.json');result=read('execution/result.json');digest=plan['observer']['policy_sha256']
    first=read('cpu-01/result.json');firstfix=read('cpu-fix/result.json');fixed=read('cpu-fix-02/result.json')
    require(not first['passed'] and first['tests_run']==33 and first['errors']==1 and first['failures']==0 and
        all(v['returncode']==0 for v in first['commands']) and first['rule_scenarios']==22,'original group result')
    require(not firstfix['passed'] and firstfix['tests_run']==1 and firstfix['failures']==1 and firstfix['errors']==0,
            'original empty PID fixture failure')
    require(fixed['passed'] and fixed['tests_run']==1 and fixed['errors']==fixed['failures']==0 and
        fixed['reused_cpu_sha256']==sha(data['cpu-01/result.json']),'targeted corrected result')
    driver=read('cpu-driver-02/result.json')
    require(driver['passed'] and driver['tests_run']==5 and driver['failures']==driver['errors']==0,'driver result')
    runtime=read('cpu-runtime/result.json')
    require(not runtime['passed'] and runtime['tests_run']==5 and runtime['failures']==1 and runtime['errors']==0 and
        runtime['reused_cpu_sha256']==sha(data['cpu-fix-02/result.json']),'original runtime cleanup failure')
    fix=read('cpu-runtime-fix/result.json')
    require(fix['passed'] and fix['tests_run']==2 and fix['failures']==fix['errors']==0 and
        fix['reused_cpu_sha256']==sha(data['cpu-runtime/result.json']),'targeted cleanup correction')
    for folder in ('cpu-01','cpu-fix','cpu-fix-02','cpu-runtime','cpu-runtime-fix','cpu-driver','cpu-driver-02'):
        value=read(folder+'/result.json')
        require(value['models_started']==value['listeners_started']==value['gpu_started']==0,'CPU no service activity')
        require(sha(data[folder+'/result.json'])==plan['receipts'][folder],'receipt binding')
        for name,checksum in value['sources'].items():
            require(sha(data[folder+'/source/'+name])==checksum,'CPU source bytes')
            changed={'tools/serve_host/observer.py','tools/serve_host/child.py','tools/serve_host/stage.py','tools/serve_host/report.py'}
            if folder=='cpu-driver':changed.update(('tools/serve_host/stage.py','tools/serve_host/report.py','tools/serve_host/driver_cpu.py'))
            if folder=='cpu-fix':changed.add('tools/serve_host/cpu_fix.py')
            if (folder in ('cpu-runtime-fix','cpu-driver-02') and name not in ('tools/serve_host/stage.py','tools/serve_host/report.py')) or name not in changed or (
                    folder in ('cpu-fix-02','cpu-runtime','cpu-driver') and name=='tools/serve_host/child.py'):
                require(plan['source_files'][name]==checksum,'unaffected source reuse')
    require(plan['reused_archive_sha256']==first['reused_host_evidence_sha256']==
        '22a44409eea5ad5ec86bfea01490f1de48d69578a9d788dc5207a46077e96c20','unchanged prior kernel/business evidence')
    require(result['status']=='PASS_HOST_OBSERVATION_SHORT' and result['cleanup_confirmed'] and result['seconds']<=240 and
        result['counts']['generations']==4 and result['counts']['controls']<=64 and not result['long_run'] and
        not result['performance_acceptance'] and not result['external_notifications'],'bounded real stage')
    require(sha(data['observer/policy.json'])==digest,'observer package identity')
    policy=read('observer/policy.json')
    require(policy['backend_manifest_sha256']==plan['package']['manifest_sha256'] and
        policy['backend_run']==str(BASE/'execution/managed-run') and policy['host_resources']==plan['host_resources'],
        'pinned source binding')
    binding=read('observer/binding.json')
    require(binding['controller_sha256']==sha(data['execution/managed-run/controller.json']) and
        binding['resource_sha256']==sha(data['execution/managed-run/resources.json']),'sticky source identity')
    for label,state,kind in (('running','RUNNING','current'),('recovered','RUNNING','current'),('final','STOPPED','final')):
        value=read('execution/'+label+'-projection.json')
        require(value['state']==state and value['kind']==kind and value['reason']=='none','actual source disposition')
        wire=data['execution/'+label+'-metrics.prom']
        require(s.projection_matches(wire,value,'thor-host-short',digest),'exact public metric series')
        require(str(BASE).encode() not in wire and b'pid=' not in wire and b'api_key' not in wire,'public metric privacy')
        if kind=='current':
            raw=read('execution/'+label+'-kernel.json')
            require(raw['effective_limits']==value['resources']['effective_limits'] and
                s.prior.resource.violation(raw) is None and value['resources']['memory.current']>0 and
                value['resources']['pids.current']>0,'real kernel sample')
    for state in ('RUNNING','STOPPED'):
        rows=read('execution/query-'+state+'.json')['data']['result']
        require(len(rows)==1 and rows[0]['metric']['generation']==digest and rows[0]['value'][1]=='1','Prometheus state')
    rows=read('execution/query-memory.json')['data']['result']
    require(len(rows)==1 and rows[0]['metric']['generation']==digest and float(rows[0]['value'][1])>0,'Prometheus kernel metric')
    paused=read('execution/paused.json')
    for name,alert,present in (('initial-alerts','Q4THostObserverStale',False),('stale-alert','Q4THostObserverStale',True),
                             ('recovered-alert','Q4THostObserverStale',False),('terminal-alert','Q4THostGenerationTerminal',True)):
        value=read('execution/'+name+'.json')
        rows=[v for v in value['response']['data']['alerts'] if v['labels']['alertname']==alert and v['state']=='firing']
        require(bool(rows)==present and all(v['labels']['generation']==digest for v in rows),'actual alert transition')
    apis=[read(n) for n in sorted(data) if n.startswith('execution/api-')]
    wire=next(v for v in apis if v['label']=='paused-still-served')
    require(sha(wire['body'].encode())==paused['metrics_sha256'] and
        wire['unix']-paused['projection']['evaluated_unix']>30,'stale static file actually served')
    require(read('execution/recovered-projection.json')['evaluated_unix']>paused['projection']['evaluated_unix'],
            'resumed observer freshness')
    same=read('execution/source-read-only.json')
    require(same['passed'] and same['before']==same['after'],'source was modified')
    for name,checksum in same['after'].items():require(sha(data['execution/managed-run/'+name])==checksum,'ended source bytes')
    terminal=read('execution/managed-run/terminal.json');final=read('execution/managed-run/resources-final.json')
    require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and terminal['guard']['status']=='COMPLETED' and
        terminal['host_resource_cleanup']['scope_removed'] and terminal['worker']['attempts']==1 and terminal['worker']['restarts']==0 and
        final['payload_removed'] and final['violation'] is None,'managed final cleanup')
    require(read('execution/final-projection.json')['resources']==s.o.c.snapshot(final['final'],plan['host_resources']),
            'historical final numeric evidence')
    log=data['execution/managed-run/backend.log'].decode()
    ends=[dict(re.findall(r'([a-z_]+)=([^ ]+)',line)) for line in log.splitlines() if line.startswith('[q4t][request]') and ' event=end ' in line]
    require(len(ends)==4 and all(v['outcome']=='success' and v['reason']=='completed' and v['released']=='1' for v in ends) and
        log.count('grammar worker reaped=1 ')==4 and 'shutdown complete (0 in-flight remaining)' in log,'backend completions')
    metrics=read('execution/final-metrics.json');s.retained.a.healthy_idle(metrics,4)
    require(metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4,'grammar metrics')
    usage=tasks(data,plan,ends)
    fronts=[name.rsplit('/',1)[0] for name in data if name.startswith('owned-generations/generations/') and name.endswith('/reservation.json')]
    require(len(fronts)==1 and read(fronts[0]+'/reservation.json')['kind']=='gateway','one TLS gateway')
    rows=[json.loads(line) for line in data[fronts[0]+'/run/gateway.log'].splitlines() if line.startswith(b'{')]
    require(len(rows)==10 and all(sum(v['client']==client and v['status']==200 and v['upstream_status']=='200' and
        v['lane']==lane for v in rows)==count for client in ('alpha','beta') for lane,count in (('data',4),('control',1))),
        'TLS independent clients')
    events=[json.loads(line) for line in data['execution/managed-run/lease/events.jsonl'].splitlines()]
    observations=[v for v in events if v['event'] in ('preflight','monitor')]
    require(events[0]['event']=='lease_acquired' and events[-1]['event']=='lease_released' and events[-1]['status']=='COMPLETED' and
        all(not v['conflicts'] for v in observations) and all(0<=b['monotonic']-a['monotonic']<=10 for a,b in zip(events,events[1:])),
        'original GPU monitor')
    custody=[json.loads(line) for line in data['execution/custody/events.jsonl'].splitlines()]
    exit_=read('execution/custody/exit.json');copy=read('execution/custody/lease-copy.json')
    require(exit_['status']=='COMPLETED' and exit_['cleanup_confirmed'] and exit_['samples']==len(custody)>0 and
        copy['device']==66305 and copy['inode']==exit_['lock_inode']==3294067 and
        all(not row['observation']['conflicts'] for row in custody) and
        all(0<=b['monotonic']-a['monotonic']<=10 for a,b in zip(custody,custody[1:])),'same lease custody monitor')
    require(events[0]['monotonic']<custody[0]['monotonic']<events[-1]['monotonic']<custody[-1]['monotonic'] and
        all(custody[0]['monotonic']<=row['monotonic']<=custody[-1]['monotonic'] for row in apis),'HTTP within continuous lease')
    gpu_seconds=result['gpu_finished']-result['gpu_started'];require(gpu_seconds<=210,'GPU custody budget')
    post=read('postflight/result.json')
    require(post['passed'] and post['lock_available'] and post['sealed_generations']==1,'postflight')
    return dict(status='ACCEPT_HOST_OBSERVATION_SHORT',effective_cpu_tests=37,driver_tests=5,rule_scenarios=22,
        original_nginx_failures_preserved=2,original_cleanup_failure_preserved=1,
        model_starts=1,gateway_starts=1,observer_starts=1,monitor_starts=1,
        responses=4,real_tools=2,completed_tasks=2,response_usage=usage,grammar_reaped=4,
        memcg_peak_bytes=final['final']['memory.peak'],memcg_events=final['final']['memory.events'],
        pids_events=final['final']['pids.events'],stale_firing=True,freshness_recovered=True,terminal_firing=True,
        paused_seconds=read('execution/recovered-alert.json')['monotonic']-paused['monotonic'],
        controls=result['counts']['controls'],gpu_seconds=gpu_seconds,guard_observations=len(observations),
        custody_observations=len(custody),seconds=result['seconds'],owned_exited=post['owned_exited'],
        source_read_only=True,external_notifications=False,physical_ram_limit_proved=False,
        long_run=False,performance_acceptance=False,cleanup_confirmed=True)


def postflight():
    s.configure();plan=s.check();output=BASE/'postflight';output.mkdir(mode=0o700)
    s.retained.a.require_idle(output/'gpu.json');lock=s.retained.a.guard.common_lock();info=lock.stat()
    require([info.st_dev,info.st_ino]==[66305,3294067] and s.retained.a.lock_available(lock),'original GPU lock')
    value=subprocess.run(['systemctl','--user','show','soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service',
        '-p','LoadState','-p','ActiveState','-p','SubState','-p','MainPID'],capture_output=True,text=True,timeout=10,check=True)
    require(dict(line.split('=',1) for line in value.stdout.splitlines())==
        dict(LoadState='masked',ActiveState='inactive',SubState='dead',MainPID='0'),'SOAR changed')
    (output/'soar-unit.txt').write_text(value.stdout)
    value=subprocess.run(['sudo','-n','docker','ps','--format','{{.Names}}'],capture_output=True,text=True,timeout=10,check=True)
    require(not value.stdout.strip(),'foreign containers; preserve');(output/'containers.txt').write_text(value.stdout)
    with s.retained.store.locked(BASE/'owned-generations') as (path,meta):rows=s.retained.store.inventory(path,meta)
    require(len(rows)==1 and all(v['state']=='SEALED' for v in rows),'gateway generation cleanup')
    owners=[]
    for path in (BASE/'owned-generations/generations').iterdir():
        owners.extend([pkg.read(path/'reservation.json')['owner'],*pkg.read(path/'seal.json')['proof']['owners']])
    owners+=pkg.read(BASE/'execution/backend-ownership.json')['owners']
    owners += [pkg.read(BASE/'execution'/name)['owner'] for name in ('observer-owner.json','prometheus-owner.json')]
    owners += [pkg.read(BASE/'observer/exporter.json')['owner'],pkg.read(BASE/'execution/prometheus-run/status.json')['child']]
    unique={(v['pid'],v['start_ticks'],v['boot_id']):v for v in owners}
    require(all(not s.retained.r.owner_live(v) for v in unique.values()),'owned process remained')
    for port in (*s.PORTS.values(),pkg.verify(plan['package']['package'],plan['package']['manifest_sha256'])['settings']['port'],
                 s.retained.r.gateway.g.parse(s.retained.r.gateway.g.read_file(s.retained.prior.BASE/'model-private/policy-private.json',private=True))['listen_port']):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind(('127.0.0.1',port))
    with s.o.Directory(BASE/'observer') as directory,directory.claim('observer.lock',0):pass
    with s.n.claim(BASE/'monitor-state','prometheus'):pass
    pkg.save(output/'result.json',dict(passed=True,owners=list(unique.values()),owned_exited=len(unique),inventory=rows,
        sealed_generations=1,lock_available=True,lock_identity=[info.st_dev,info.st_ino],ports_available=True,
        default_unchanged=True,day_plans_unchanged=True,days=plan['days'],new_models_or_http=0))
    return dict(passed=True,owned_exited=len(unique),sealed_generations=1)


def collect():
    paths=[BASE/'plan.json',BASE/'live-command.log']
    for folder in ('cpu-01','cpu-fix','cpu-fix-02','cpu-runtime','cpu-runtime-fix','cpu-driver','cpu-driver-02',
                   'execution','owned-generations','tasks','postflight','cpu-continuation','cpu-projection-fix',
                   'original-before-gpu','original-projection-failure'):
        paths.extend(p for p in (BASE/folder).rglob('*') if p.is_file())
    paths.extend((BASE/'observer'/name) for name in ('policy.json','nginx.conf','syntax-private.log','observer.json',
        'binding.json','status.json','metrics.prom','exporter.json','exporter.log','observer-terminal.json'))
    plan=pkg.read(BASE/'plan.json');notification=Path(plan['notification']['package'])
    records=('manifest.json','prometheus.json','alertmanager.json','notify.rules.json','validation-0-private.log','validation-1-private.log')
    paths.extend((BASE/'cpu-fixtures').glob('*/observer/syntax-private.log'))
    data={str(p.relative_to(BASE)):p.read_bytes() for p in sorted(set(paths)) if p.is_file() and not p.is_symlink()}
    data.update({'notification/'+name:(notification/name).read_bytes() for name in records})
    # Reconstruction tooling can be corrected after the runtime is frozen.
    # Archive its exact bytes separately; never rewrite the tested sources.
    for name in ('tools/serve_host/report.py','tools/serve_host/projection_fix.py','tools/serve_host/stage.py'):
        data['reconstruction-source/'+name]=(ROOT/name).read_bytes()
    policy=pkg.read(s.retained.prior.BASE/'model-private/policy-private.json')
    secrets=[v['api_key'] for v in policy['clients']]+[pkg.read(s.retained.prior.BASE/'model-private/clients.json')['gateway_token'],
        (notification/'token-private').read_text().strip()]
    for name,raw in data.items():
        require(all(secret.encode() not in raw for secret in secrets) and not any(b'-----BEGIN '+k+b'PRIVATE KEY-----' in raw
            for k in (b'',b'RSA ',b'EC ',b'ENCRYPTED ')),'secret in public evidence: '+name)
    paths=[Path(plan['package']['package'])/'q4t',Path(plan['gateways'][0]['package'])/'nginx',BASE/'observer/nginx',
           *(notification/name for name in s.n.BINARIES)]
    local={str(p):dict(bytes=p.stat().st_size,sha256=pkg.sha(p)) for p in paths}
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
        local_identities=local,archive=dict(bytes=len(packed),sha256=sha(packed)),
        reconstruction_sources={name.removeprefix('reconstruction-source/'):sha(raw)
            for name,raw in data.items() if name.startswith('reconstruction-source/')}))
    pkg.save(DEST/'result.json',result);return dict(records=len(data),compressed_bytes=len(packed),result=result)


def verify():
    manifest=pkg.read(DEST/'manifest.json');packed=(DEST/'records.tar.gz').read_bytes()
    for name,checksum in manifest['reconstruction_sources'].items():
        require(pkg.sha(ROOT/name)==checksum,'use archived reconstruction source: '+name)
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
