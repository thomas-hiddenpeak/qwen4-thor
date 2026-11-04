"""Offline reconstruction of TLS renewal, real tasks and owned cleanup."""
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
from tools.serve_gateway import tls_stage as s,tls
from tools.serve_service import package as pkg
from tools.serve_host import report as business

BASE=s.BASE;DEST=ROOT/'docs/evidence/serve-tls-lifecycle-20261104'
require=pkg.require
sha=lambda raw:hashlib.sha256(raw).hexdigest()


def evaluate(data):
    read=lambda name:json.loads(data[name])
    plan=read('plan.json');result=read('execution/result.json')
    first=read('cpu-driver/result.json')
    require(not first['passed'] and first['tests_run']==4 and first['errors']==1 and first['failures']==0 and
        b'RuntimeError: formal cleanup failed' in data['cpu-driver/tests.log'],'original driver assertion failure')
    for name,digest in first['sources'].items():
        require(sha(data['cpu-driver/source/'+name])==digest,'original driver source')
        if name not in ('tools/serve_gateway/tls_driver_cpu.py','tools/serve_gateway/tls_stage.py','tools/serve_gateway/tls_report.py'):
            require(plan['source_files'][name]==digest,'unchanged driver dependency')
    require(sha(data['cpu-driver/result.json'])==plan['receipts']['cpu-driver'],'original driver receipt')
    for folder,count in (('cpu-01',61),('cpu-driver-fix',1)):
        value=read(folder+'/result.json')
        require(value['passed'] and value['tests_run']==count and value['errors']==value['failures']==0 and
            value['models_started']==value['http_started']==value['gpu_started']==0,'CPU outcome')
        require(sha(data[folder+'/result.json'])==plan['receipts'][folder],'CPU receipt identity')
        for name,digest in value['sources'].items():
            require(sha(data[folder+'/source/'+name])==digest==plan['source_files'][name],'tested source identity')
    require(result['status']=='PASS_TLS_RENEWAL_SHORT' and result['cleanup_confirmed'] and result['seconds']<=240 and
        result['counts']['generations']==4 and result['counts']['controls']<=64 and result['tls_probes']==2 and
        not result['long_run'] and not result['performance_acceptance'],'bounded real result')
    roots=[n.rsplit('/',1)[0] for n in data if n.startswith('owned-generations/generations/') and n.endswith('/reservation.json')]
    require(len(roots)==3,'one backend and two gateway lifetimes')
    backend=next(n for n in roots if read(n+'/reservation.json')['kind']=='backend')
    fronts=[n for n in roots if n!=backend]
    terminal=read(backend+'/run/terminal.json');final=read(backend+'/run/resources-final.json')
    require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and terminal['worker']['attempts']==1 and
        terminal['worker']['restarts']==0 and terminal['host_resource_cleanup']['scope_removed'] and
        terminal['guard']['status']=='COMPLETED' and final['payload_removed'] and final['violation'] is None and
        not final['final']['members'],'backend resource cleanup')
    for name in roots:require(read(name+'/seal.json')['proof']['cleanup_confirmed'],'sealed generation')
    gateway_rows=[]
    for name in fronts:
        state=read(name+'/run/terminal.json')
        require(state['state']=='STOPPED' and state['cleanup_confirmed'] and state['starts']==1 and state['restarts']==0 and
            not state['backend_stopped_by_gateway'] and state['tls']['state']=='VALID','gateway lifetime')
        gateway_rows.extend(json.loads(line) for line in data[name+'/run/gateway.log'].splitlines() if line.startswith(b'{'))
    require(len(gateway_rows)==10 and all(sum(v['client']==c and v['status']==200 and v['upstream_status']=='200' and
        v['lane']==lane for v in gateway_rows)==count for c in ('alpha','beta') for lane,count in (('data',4),('control',1))),
        'real authenticated gateway traffic')
    fingerprints=[]
    for index in (1,2):
        outer=read(f'gateway-{index}/manifest.json');inner=read(f'gateway-{index}/gateway/manifest.json')
        require(sha(data[f'gateway-{index}/manifest.json'])==plan['gateways'][index-1]['manifest_sha256'] and
            sha(data[f'gateway-{index}/gateway/manifest.json'])==outer['gateway']['manifest_sha256'] and
            outer['version']==4 and inner['version']==3,'TLS package identity')
        cert=data[f'gateway-{index}/gateway/certificate.pem'];ca=data[f'gateway-{index}/gateway/trusted-ca.pem']
        require(sha(cert)==inner['files']['certificate.pem'] and sha(ca)==inner['files']['trusted-ca.pem']==plan['ca']['sha256'],
            'public TLS material identity')
        observed=read(f'execution/front-{index}-tls.json');fingerprint=tls.certificates(cert)[0][1];fingerprints.append(fingerprint)
        require(observed['verified'] and observed['leaf_sha256']==fingerprint==inner['tls']['chain'][0]['sha256'] and
            observed['ca_sha256']==plan['ca']['sha256'] and observed['hostname']==inner['policy']['server_name'] and
            observed['protocol'] in ('TLSv1.2','TLSv1.3'),'actual served leaf/trust/hostname')
        for name,digest in outer['sources'].items():require(digest==plan['source_files'][name],'pinned production source')
    require(len(set(fingerprints))==2,'leaf did not change')
    renewal=read('execution/renewal.json')
    require(renewal['before_backend']==renewal['after_backend']==read(backend+'/run/attempt-1.json')['owner'] and
        renewal['same_ca'] and renewal['old_gateway_stopped'] and not renewal['zero_downtime_claim'],'same-backend renewal')
    log=data[backend+'/run/backend.log'].decode()
    ends=[dict(re.findall(r'([a-z_]+)=([^ ]+)',line)) for line in log.splitlines() if line.startswith('[q4t][request]') and ' event=end ' in line]
    require(len(ends)==4 and all(v['outcome']=='success' and v['reason']=='completed' and v['released']=='1' for v in ends) and
        log.count('grammar worker reaped=1 ')==4 and 'shutdown complete (0 in-flight remaining)' in log,'backend request evidence')
    metrics=read('execution/final-metrics.json');s.a.healthy_idle(metrics,4)
    require(metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4,'grammar lifecycle')
    business.BASE=BASE;usage=business.tasks(data,plan,ends)
    events=[json.loads(row) for row in data[backend+'/run/lease/events.jsonl'].splitlines()]
    observations=[v for v in events if v['event'] in ('preflight','monitor')]
    require(events[0]['event']=='lease_acquired' and events[-1]['event']=='lease_released' and
        events[-1]['status']=='COMPLETED' and observations and all(v['conflicts']==[] for v in observations) and
        all(0<=b['monotonic']-a['monotonic']<=10 for a,b in zip(events,events[1:])),'continuous GPU lease')
    seconds=events[-1]['monotonic']-events[0]['monotonic'];require(seconds<=210,'GPU bound')
    require(read('execution/guard-archive.json')==dict(passed=True,records=len(events),observations=len(observations),seconds=seconds),
        'independent guard reconstruction')
    post=read('postflight/result.json')
    require(post['passed'] and post['sealed_generations']==3 and post['lock_available'] and post['ports_available'] and
        post['lock_identity']==[66305,3294067] and post['days']==plan['days'] and post['default_unchanged'] and
        read('postflight/gpu.json')['pids']==[] and not data['postflight/containers.txt'].strip(),'postflight')
    properties=dict(line.split('=',1) for line in data['postflight/soar-unit.txt'].decode().splitlines())
    require(properties==dict(LoadState='masked',ActiveState='inactive',SubState='dead',MainPID='0'),'recovery service remained off')
    require(read('old-entry.json')['returncode']==0 and read('old-entry.json')['listeners_started']==0,'old pinned entry')
    return dict(passed=True,cpu_tests=61,driver_tests=4,driver_correction_tests=1,original_assertion_error_preserved=True,
        models=1,gateways=2,successful_responses=4,real_tools=2,
        completed_tasks=2,response_usage=usage,leaf_sha256=fingerprints,renewal_seconds=renewal['seconds'],
        gpu_seconds=seconds,gpu_observations=len(observations),seconds=result['seconds'],controls=result['counts']['controls'],
        memcg_peak_bytes=final['final']['memory.peak'],memcg_events=final['final']['memory.events'],pids_events=final['final']['pids.events'],
        owned_exited=post['owned_exited'],sealed_generations=3,cleanup_confirmed=True,long_run=False,
        actual_lan_accepted=False,zero_downtime_claim=False,actual_waited_expiry=False,performance_acceptance=False,
        original_offline_terminal_identity_error_preserved=True)


def postflight():
    s.configure();plan=s.check();output=BASE/'postflight';output.mkdir(mode=0o700)
    s.a.require_idle(output/'gpu.json');lock=s.a.guard.common_lock();info=lock.stat()
    require([info.st_dev,info.st_ino]==[66305,3294067] and s.a.lock_available(lock),'original GPU lock')
    result=subprocess.run(['systemctl','--user','show','soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service',
        '-p','LoadState','-p','ActiveState','-p','SubState','-p','MainPID'],capture_output=True,text=True,timeout=10,check=True)
    require(dict(line.split('=',1) for line in result.stdout.splitlines())==
        dict(LoadState='masked',ActiveState='inactive',SubState='dead',MainPID='0'),'SOAR changed')
    (output/'soar-unit.txt').write_text(result.stdout)
    result=subprocess.run(['sudo','-n','docker','ps','-a','--format','{{.Names}}'],capture_output=True,text=True,timeout=10,check=True)
    require(not result.stdout.strip(),'foreign containers; preserve');(output/'containers.txt').write_text(result.stdout)
    with s.retained.store.locked(BASE/'owned-generations') as (path,meta):rows=s.retained.store.inventory(path,meta)
    require(len(rows)==3 and all(v['state']=='SEALED' for v in rows),'generation cleanup')
    owners={}
    for path in (BASE/'owned-generations/generations').iterdir():
        for owner in [pkg.read(path/'reservation.json')['owner'],*pkg.read(path/'seal.json')['proof']['owners']]:
            owners[owner['pid'],owner['start_ticks'],owner['boot_id']]=owner
    require(all(not s.retained.r.owner_live(v) and not s.sup.processes.group_members(v['pid']) for v in owners.values()),'owned process remained')
    for port in (18128,18448):
        with socket.socket() as sock:sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind(('127.0.0.1',port))
    release=pkg.read(ROOT/'build/q4t.release.json');runtime=pkg.read(release['tested_source_manifest'])
    require(all(pkg.sha(ROOT/n)==digest for n,digest in runtime.items()),'runtime source changed')
    pkg.save(output/'result.json',dict(passed=True,owners=list(owners.values()),owned_exited=len(owners),inventory=rows,
        sealed_generations=3,lock_available=True,lock_identity=[info.st_dev,info.st_ino],ports_available=True,
        default_unchanged=True,day_plans_unchanged=True,days=plan['days'],runtime_sources=runtime,new_models_or_http=0))
    return dict(passed=True,owned_exited=len(owners),sealed_generations=3)


def collect():
    paths=[BASE/n for n in ('plan.json','old-entry.json','live-command.log','archive-failure.log','preflight-gpu.json')]
    for folder in ('cpu-01','cpu-driver','cpu-driver-fix','execution','owned-generations','tasks','postflight'):
        paths.extend(p for p in (BASE/folder).rglob('*') if p.is_file())
    for index in (1,2):
        paths.extend(BASE/f'gateway-{index}'/n for n in ('manifest.json','gateway/manifest.json','gateway/certificate.pem','gateway/trusted-ca.pem'))
    data={str(p.relative_to(BASE)):p.read_bytes() for p in sorted(set(paths)) if not p.is_symlink()}
    data['reconstruction/tools/serve_gateway/tls_report.py']=Path(__file__).read_bytes()
    private=ROOT/'.q4t-work/serve-clients-managed-20261104/model-private'
    secrets=[v['api_key'] for v in pkg.read(private/'policy-private.json')['clients']]+[pkg.read(private/'clients.json')['gateway_token']]
    for name,raw in data.items():
        require(all(v.encode() not in raw for v in secrets) and not any(b'-----BEGIN '+k+b'PRIVATE KEY-----' in raw
            for k in (b'',b'RSA ',b'EC ',b'ENCRYPTED ')),'secret in public evidence: '+name)
    plan=pkg.read(BASE/'plan.json')
    files=[Path(plan['package']['package'])/'q4t',*(BASE/f'gateway-{i}/nginx' for i in (1,2)),tls.OPENSSL,Path(sys.executable).resolve()]
    local={str(p):dict(bytes=p.stat().st_size,sha256=pkg.sha(p)) for p in files}
    return data,local


def archive():
    data,local=collect();result=evaluate(data);DEST.mkdir()
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w') as target:
        for name,raw in sorted(data.items()):
            info=tarfile.TarInfo(name);info.size,info.mode,info.mtime=len(raw),0o600,0;target.addfile(info,io.BytesIO(raw))
    packed=gzip.compress(stream.getvalue(),mtime=0);(DEST/'records.tar.gz').write_bytes(packed)
    pkg.save(DEST/'manifest.json',dict(files={n:dict(bytes=len(raw),sha256=sha(raw)) for n,raw in data.items()},
        local_identities=local,archive=dict(bytes=len(packed),sha256=sha(packed)),
        reconstruction=dict(path='tools/serve_gateway/tls_report.py',sha256=pkg.sha(__file__))))
    pkg.save(DEST/'result.json',result)
    return dict(records=len(data),compressed_bytes=len(packed),result=result)


def verify():
    manifest=pkg.read(DEST/'manifest.json');packed=(DEST/'records.tar.gz').read_bytes()
    require(pkg.sha(__file__)==manifest['reconstruction']['sha256'],'use the pinned reconstruction tool')
    require(len(packed)==manifest['archive']['bytes'] and sha(packed)==manifest['archive']['sha256'],'archive identity')
    data={}
    with tarfile.open(fileobj=io.BytesIO(packed),mode='r:gz') as source:
        for member in source:
            require(member.isfile() and member.name not in data and member.name in manifest['files'],'archive member')
            raw=source.extractfile(member).read();expected=manifest['files'][member.name]
            require(len(raw)==expected['bytes'] and sha(raw)==expected['sha256'],'record identity');data[member.name]=raw
    require(data.keys()==manifest['files'].keys() and evaluate(data)==pkg.read(DEST/'result.json'),'offline result differs')
    return dict(passed=True,records=len(data),new_models_or_http=0)


if __name__=='__main__':
    os.umask(0o077);parser=argparse.ArgumentParser();parser.add_argument('action',choices=('postflight','archive','verify'))
    print(json.dumps(globals()[parser.parse_args().action]()))
