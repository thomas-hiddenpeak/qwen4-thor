"""Reconstruct real TLS observation and local delivery from bounded raw records."""
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

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_tls import stage as s
from tools.serve_service import package as pkg
from tools.serve_host import report as business
from tools.serve_gateway import tls

BASE=s.BASE;DEST=ROOT/'docs/evidence/serve-tls-observation-20261104'
require=pkg.require
sha=lambda raw:hashlib.sha256(raw).hexdigest()


def evaluate(data):
    read=lambda name:json.loads(data[name]);plan=read('plan.json');result=read('execution/result.json')
    first=read('cpu-01/result.json');fixed=read('cpu-rule-fix/result.json');driver=read('cpu-driver/result.json')
    require(not first['passed'] and first['tests_run']==57 and first['errors']==first['failures']==0 and
        {v['label']:v['returncode'] for v in first['commands']}==dict(rules=0,series=1,metrics=0,render=0) and
        b'NaNx80' in data['cpu-01/series.stderr'],'original rule fixture parse failure')
    require(fixed['passed'] and fixed['returncode']==0 and fixed['rule_scenarios']==29 and
        fixed['original_cpu_sha256']==sha(data['cpu-01/result.json']) and b'SUCCESS' in data['cpu-rule-fix/series.stdout'],
        'corrected official rule scenarios')
    require(driver['passed'] and driver['tests_run']==4 and driver['errors']==driver['failures']==0,'driver checks')
    for folder in ('cpu-01','cpu-rule-fix','cpu-driver'):
        value=read(folder+'/result.json')
        require(value['models_started']==value['http_started']==value['gpu_started']==0 and
            sha(data[folder+'/result.json'])==plan['receipts'][folder],'CPU receipt identity/no service')
        for name,digest in value['sources'].items():
            require(sha(data[folder+'/source/'+name])==digest,'CPU source bytes')
            if not (folder=='cpu-01' and name=='tools/serve_tls/cpu.py'):
                require(digest==plan['source_files'][name],'tested runtime dependency changed: '+name)
    require(result['status']=='PASS_TLS_OBSERVATION_SHORT' and result['cleanup_confirmed'] and result['seconds']<=240 and
        result['counts']['generations']==4 and result['counts']['controls']<=64 and
        not result['long_run'] and not result['performance_acceptance'] and not result['external_notifications'],'real bounded result')
    outer=read('gateway/manifest.json');inner=read('gateway/gateway/manifest.json');policy=read('observer/policy.json')
    digest=plan['observer']['policy_sha256']
    require(outer['version']==4 and inner['version']==3 and sha(data['gateway/manifest.json'])==plan['gateways'][0]['manifest_sha256'] and
        sha(data['gateway/gateway/manifest.json'])==outer['gateway']['manifest_sha256'] and
        sha(data['observer/policy.json'])==digest and policy['gateway_manifest_sha256']==plan['gateways'][0]['manifest_sha256'] and
        policy['gateway_run']==str(BASE/'execution/gateway-run') and policy['material']==inner['tls'],'pinned observation package')
    require(sha(data['gateway/gateway/certificate.pem'])==inner['files']['certificate.pem'] and
        sha(data['gateway/gateway/trusted-ca.pem'])==inner['files']['trusted-ca.pem']==plan['ca']['sha256'],'public material identity')
    require([tls.dates(*v) for v in tls.certificates(data['gateway/gateway/certificate.pem'])]==inner['tls']['chain'],'actual public dates')
    for label,state in (('running','READY'),('recovered','READY'),('terminal','STOPPED')):
        value=read('execution/'+label+'-projection.json');raw=data['execution/'+label+'-metrics.prom']
        require(value['state']==state and value['reason']=='none' and value['certificate']['state']=='EXPIRING' and
            value['certificate']==s.o.c.certificate(inner['tls'],value['evaluated_unix']) and
            sorted(raw.splitlines())==sorted(s.o.c.metrics(value,'thor-acceptance',digest).encode().splitlines()),'observed projection')
    binding=read('observer/binding.json');control=read('execution/gateway-run/controller.json')
    require(binding['controller']==control['owner'] and binding['controller_sha256']==sha(data['execution/gateway-run/controller.json']) and
        binding['nginx']==read('execution/gateway-run/status.json')['nginx'],'sticky original source identities')
    snapshot=read('execution/gateway-source-after-stop.json')
    require(all(sha(data['execution/gateway-run/'+name])==digest for name,digest in snapshot.items()),'read-only terminated source')
    gateway=read('execution/gateway-run/terminal.json')
    require(gateway['state']=='STOPPED' and gateway['cleanup_confirmed'] and gateway['starts']==1 and gateway['restarts']==0 and
        not gateway['backend_stopped_by_gateway'] and not gateway['automatic_reactivation'],'gateway clean terminal')
    notifications=[]
    for label,name,firing in (('expiring','Q4TTLSCertificateExpiring',True),('stale','Q4TTLSObserverStale',True),
            ('recovered','Q4TTLSObserverStale',False),('terminal','Q4TTLSGenerationTerminal',True)):
        alert=read('execution/'+label+'-alert.json')
        rows=[v for v in alert['response']['data']['alerts'] if v['labels']['alertname']==name and v['state']=='firing']
        require(alert['name']==name and alert['firing']==firing and bool(rows)==firing and
            all(v['labels']['generation']==digest for v in rows),'actual Prometheus state')
        delivery=read('execution/'+label+'-delivery.json');status='firing' if firing else 'resolved'
        require(delivery['name']==name and delivery['status']==status and delivery['receipt_indices'],'delivery selection')
        for index in delivery['receipt_indices']:
            row=read(f'execution/receiver/{index:03d}-receipt.json');raw=data[f'execution/receiver/{index:03d}-body.json']
            require(row['sha256']==sha(raw) and row['payload']==json.loads(raw) and row['authorization_valid'] and
                row['response_status']==204 and row['path']=='/alerts','actual authenticated HTTPS receipt')
            selected=[v for v in row['payload']['alerts'] if v['labels']['alertname']==name and v['status']==status]
            require(selected and all(v['labels']['generation']==digest and v['labels']['deployment']=='thor-acceptance' and
                set(v['labels'])<=set(('alertname','severity','component','deployment','job','instance','generation')) for v in selected),
                'delivery generation/finite labels')
        notifications.append(dict(name=name,status=status,receipts=delivery['receipt_indices']))
    paused=read('execution/paused.json')
    wire=next(read(name) for name in data if name.startswith('execution/api-') and read(name)['label']=='paused-still-served')
    require(sha(wire['body'].encode())==paused['metrics_sha256'] and
        wire['unix']-paused['projection']['evaluated_unix']>30 and
        read('execution/recovered-projection.json')['evaluated_unix']>paused['projection']['evaluated_unix'],'frozen timestamp and recovery')
    notify=read('notification/manifest.json')
    require(notify['version']==3 and notify['policy_version']==3 and sha(data['notification/manifest.json'])==plan['notification']['manifest_sha256'],
        'new independent notification package')
    prom=read('notification/prometheus.json');am=read('notification/alertmanager.json')
    require(any(v['job_name']=='q4t-tls' for v in prom['scrape_configs']) and
        'generation' in am['route']['group_by'],'TLS scrape and generation grouping')
    backend='execution/managed-run';terminal=read(backend+'/terminal.json');final=read(backend+'/resources-final.json')
    require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and terminal['worker']['attempts']==1 and
        terminal['worker']['restarts']==0 and terminal['host_resource_cleanup']['scope_removed'] and
        terminal['guard']['status']=='COMPLETED' and final['payload_removed'] and final['violation'] is None and
        not final['final']['members'],'backend cleanup')
    require(control['binding']['backend']==read(backend+'/attempt-1.json')['owner'],'gateway bound original backend identity')
    for name in ('observer/observer-terminal.json','execution/prometheus-run/terminal.json','execution/alertmanager-run/terminal.json'):
        value=read(name);require(value['state']=='STOPPED' and value['cleanup_confirmed'] and
            not value['model_signalled'] and not value['automatic_reactivation'],'auxiliary owned cleanup')
    log=data[backend+'/backend.log'].decode()
    ends=[dict(re.findall(r'([a-z_]+)=([^ ]+)',line)) for line in log.splitlines() if line.startswith('[q4t][request]') and ' event=end ' in line]
    require(len(ends)==4 and all(v['outcome']=='success' and v['reason']=='completed' and v['released']=='1' for v in ends) and
        log.count('grammar worker reaped=1 ')==4 and 'shutdown complete (0 in-flight remaining)' in log,'real backend request/reap')
    metrics=read('execution/final-metrics.json');s.a.healthy_idle(metrics,4)
    require(metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4,'grammar balance')
    business.BASE=BASE;usage=business.tasks(data,plan,ends)
    events=[json.loads(v) for v in data[backend+'/lease/events.jsonl'].splitlines()]
    observations=[v for v in events if v['event'] in ('preflight','monitor')]
    require(events[0]['event']=='lease_acquired' and events[-1]['event']=='lease_released' and events[-1]['status']=='COMPLETED' and
        observations and all(v['conflicts']==[] for v in observations) and
        all(0<=b['monotonic']-a['monotonic']<=10 for a,b in zip(events,events[1:])),'continuous exclusive GPU evidence')
    seconds=events[-1]['monotonic']-events[0]['monotonic'];require(seconds<=210,'GPU bound')
    require(read('execution/guard-archive.json')==dict(passed=True,records=len(events),observations=len(observations),seconds=seconds),
        'guard archive reconstruction')
    post=read('postflight/result.json')
    require(post['passed'] and post['lock_available'] and post['ports_available'] and post['lock_identity']==[66305,3294067] and
        post['days']==plan['days'] and post['default_unchanged'] and read('postflight/gpu.json')['pids']==[] and
        not data['postflight/containers.txt'].strip(),'final GPU/lock/ports/original identities')
    properties=dict(v.split('=',1) for v in data['postflight/soar-unit.txt'].decode().splitlines())
    require(properties==dict(LoadState='masked',ActiveState='inactive',SubState='dead',MainPID='0'),'vLLM recovery remained off')
    return dict(passed=True,cpu_tests=57,driver_tests=4,official_rule_scenarios=29,original_rule_parse_failure_preserved=True,
        models=1,gateways=1,successful_responses=4,real_tools=2,completed_tasks=2,response_usage=usage,notifications=notifications,
        gpu_seconds=seconds,gpu_observations=len(observations),seconds=result['seconds'],controls=result['counts']['controls'],
        memcg_peak_bytes=final['final']['memory.peak'],memcg_events=final['final']['memory.events'],pids_events=final['final']['pids.events'],
        owned_exited=post['owned_exited'],cleanup_confirmed=True,long_run=False,actual_lan_accepted=False,
        external_notifications=False,actual_waited_expiry=False,physical_ram_limit=False,performance_acceptance=False)


def postflight():
    s.configure();plan=s.check();output=BASE/'postflight';output.mkdir(mode=0o700)
    s.a.require_idle(output/'gpu.json');lock=s.a.guard.common_lock();info=lock.stat()
    require([info.st_dev,info.st_ino]==[66305,3294067] and s.a.lock_available(lock),'original GPU lock')
    result=subprocess.run(['systemctl','--user','show','soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service',
        '-p','LoadState','-p','ActiveState','-p','SubState','-p','MainPID'],capture_output=True,text=True,timeout=10,check=True)
    require(dict(v.split('=',1) for v in result.stdout.splitlines())==dict(LoadState='masked',ActiveState='inactive',SubState='dead',MainPID='0'),
        'SOAR changed');(output/'soar-unit.txt').write_text(result.stdout)
    result=subprocess.run(['sudo','-n','docker','ps','-a','--format','{{.Names}}'],capture_output=True,text=True,timeout=10,check=True)
    require(not result.stdout.strip(),'foreign containers; preserve');(output/'containers.txt').write_text(result.stdout)
    owners={}
    def inspect(value):
        if isinstance(value,dict):
            if {'pid','start_ticks','boot_id'}<=value.keys():owners[value['pid'],value['start_ticks'],value['boot_id']]=value
            else:
                for v in value.values():inspect(v)
        elif isinstance(value,list):
            for v in value:inspect(v)
    for path in (BASE/'execution').rglob('*.json'):
        if path.stat().st_size<=2*1024**2:inspect(pkg.read(path))
    for name in ('observer.json','exporter.json','binding.json'):inspect(pkg.read(BASE/'observer'/name))
    require(owners and all(not s.o.source.live(v) and not s.sup.processes.group_members(v['pid']) for v in owners.values()),'owned process remained')
    for port in (18128,18448,*s.PORTS.values()):
        with socket.socket() as sock:sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind(('127.0.0.1',port))
    release=pkg.read(ROOT/'build/q4t.release.json');runtime=pkg.read(release['tested_source_manifest'])
    require(all(pkg.sha(ROOT/n)==digest for n,digest in runtime.items()),'runtime source changed')
    pkg.save(output/'result.json',dict(passed=True,owners=list(owners.values()),owned_exited=len(owners),
        lock_available=True,lock_identity=[info.st_dev,info.st_ino],ports_available=True,default_unchanged=True,
        day_plans_unchanged=True,days=plan['days'],runtime_sources=runtime,new_models_or_http=0))
    return dict(passed=True,owned_exited=len(owners))


def collect():
    paths=[BASE/n for n in ('plan.json','live-command.log')]
    for folder in ('cpu-01','cpu-rule-fix','cpu-driver','execution','tasks','postflight'):
        paths.extend(p for p in (BASE/folder).rglob('*') if p.is_file())
    paths.extend(p for p in (BASE/'observer').rglob('*') if p.is_file() and p.name!='nginx')
    paths.extend(BASE/'gateway'/n for n in ('manifest.json','gateway/manifest.json','gateway/certificate.pem','gateway/trusted-ca.pem'))
    paths.extend(p for p in (BASE/'notification').rglob('*') if p.is_file() and
        p.name not in (*s.n.BINARIES,'token-private','url-private'))
    data={str(p.relative_to(BASE)):p.read_bytes() for p in sorted(set(paths)) if p.exists() and not p.is_symlink()}
    data['reconstruction/tools/serve_tls/report.py']=Path(__file__).read_bytes()
    data['reconstruction/tools/serve_host/report.py']=(ROOT/'tools/serve_host/report.py').read_bytes()
    private=ROOT/'.q4t-work/serve-clients-managed-20261104/model-private'
    secrets=[v['api_key'] for v in pkg.read(private/'policy-private.json')['clients']]+[pkg.read(private/'clients.json')['gateway_token'],
        (BASE/'private/notification/token-private').read_text().strip()]
    for name,raw in data.items():
        require(all(v.encode() not in raw for v in secrets) and not any(b'-----BEGIN '+k+b'PRIVATE KEY-----' in raw
            for k in (b'',b'RSA ',b'EC ',b'ENCRYPTED ')),'secret in evidence: '+name)
    plan=pkg.read(BASE/'plan.json')
    files=[Path(plan['package']['package'])/'q4t',BASE/'gateway/nginx',BASE/'observer/nginx',tls.OPENSSL,Path(sys.executable).resolve(),
        *(BASE/'notification'/n for n in s.n.BINARIES)]
    return data,{str(p):dict(bytes=p.stat().st_size,sha256=pkg.sha(p)) for p in files}


def archive():
    data,local=collect();result=evaluate(data);DEST.mkdir()
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w') as target:
        for name,raw in sorted(data.items()):
            info=tarfile.TarInfo(name);info.size,info.mode,info.mtime=len(raw),0o600,0;target.addfile(info,io.BytesIO(raw))
    packed=gzip.compress(stream.getvalue(),mtime=0);(DEST/'records.tar.gz').write_bytes(packed)
    pkg.save(DEST/'manifest.json',dict(files={n:dict(bytes=len(raw),sha256=sha(raw)) for n,raw in data.items()},
        local_identities=local,archive=dict(bytes=len(packed),sha256=sha(packed)),
        reconstruction={n:pkg.sha(ROOT/n) for n in ('tools/serve_tls/report.py','tools/serve_host/report.py')}))
    pkg.save(DEST/'result.json',result);return dict(records=len(data),compressed_bytes=len(packed),result=result)


def verify():
    manifest=pkg.read(DEST/'manifest.json');packed=(DEST/'records.tar.gz').read_bytes()
    require(all(pkg.sha(ROOT/n)==digest for n,digest in manifest['reconstruction'].items()),'use pinned reconstruction tools')
    require(len(packed)==manifest['archive']['bytes'] and sha(packed)==manifest['archive']['sha256'],'archive identity');data={}
    with tarfile.open(fileobj=io.BytesIO(packed),mode='r:gz') as source:
        for member in source:
            require(member.isfile() and member.name not in data and member.name in manifest['files'],'archive member')
            raw=source.extractfile(member).read();expected=manifest['files'][member.name]
            require(len(raw)==expected['bytes'] and sha(raw)==expected['sha256'],'record identity');data[member.name]=raw
    require(data.keys()==manifest['files'].keys() and evaluate(data)==pkg.read(DEST/'result.json'),'offline reconstruction differs')
    return dict(passed=True,records=len(data),new_models_or_http=0)


if __name__=='__main__':
    os.umask(0o077);parser=argparse.ArgumentParser();parser.add_argument('action',choices=('postflight','archive','verify'))
    print(json.dumps(globals()[parser.parse_args().action]()))
