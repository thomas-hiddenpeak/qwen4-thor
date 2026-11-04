"""Offline resource audit and lossless secret-screened startup receipts."""
import argparse
from dataclasses import asdict
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_startup_listeners import stage as s,cpu
from tools.examples import chat_client

BASE,OUTPUT,PACKAGE=s.BASE,s.OUTPUT,s.PACKAGE
ARCHIVE=ROOT/'docs/evidence/serve-startup-listeners-20261104'
UNIT='soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'


def resources(label):
    out=BASE/label;out.mkdir()
    s.a.require_idle_gpu(out/'gpu.json')
    lock=s.s.a.guard.common_lock();st=lock.stat()
    cpu.require((st.st_dev,st.st_ino)==(66305,3294067) and s.s.a.lock_available(lock),
                'original shared GPU lock not available')
    for port in (s.PORT,s.OPS_PORT,s.TLS_PORT):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            probe.bind(('127.0.0.1',port));probe.listen(1)
    raw=subprocess.check_output(['systemctl','--user','show',UNIT,'-p','LoadState','-p','ActiveState','-p','SubState','-p','MainPID'],text=True)
    (out/'soar-unit.txt').write_text(raw)
    state=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
    cpu.require(state==dict(MainPID='0',LoadState='masked',ActiveState='inactive',SubState='dead'),'restore service changed')
    raw=subprocess.check_output(['sudo','-n','docker','ps','--format','{{.ID}} {{.Names}}'],text=True)
    (out/'containers.txt').write_text(raw);cpu.require(not raw.strip(),'unexpected running container')
    owners=[]
    if label=='postflight':
        from tools.serve_cuda_quota_integration.verify import custody
        for path in (OUTPUT/'business').glob('*/identity.json'):
            old=cpu.read(path)
            try:current=custody.proc(old['pid'])
            except FileNotFoundError:current=None
            cpu.require(current is None or current['start_ticks']!=old['start_ticks'],'own process retained')
            owners.append(dict(role=path.parent.name,pid=old['pid'],start_ticks=old['start_ticks'],gone=True))
        s.check()
        cpu.require(cpu.read(OUTPUT/'finalized.json')['status']=='DELIVERED','not finalized')
    cpu.save(out/'result.json',dict(passed=True,owned=owners,ports=[s.PORT,s.OPS_PORT,s.TLS_PORT],
        gpu_empty=True,lock=dict(dev=st.st_dev,inode=st.st_ino,available=True),
        restore_unit_masked=True,containers_empty=True,long_runs=False))


def package():
    plan=s.check()
    receipt=s.pkg.prepare(ROOT/'build/q4t',ROOT/'build/q4t.release.json',plan['model_dir'],
        BASE/'accepted-backend',port=s.PORT,restart_limit=0,client_policy=plan['policy'],operations_port=s.OPS_PORT)
    s.pkg.verify(receipt['package'],receipt['manifest_sha256'])
    cpu.save(BASE/'accepted-package.json',dict(package=receipt,managed_lifecycle_accepted=False,
        models_started=0,http=0))


def audit():
    plan=s.check();s.h.verify_seal(OUTPUT)
    final=cpu.read(OUTPUT/'finalized.json')
    cpu.require(final['status']=='DELIVERED','not delivered')
    s.a.verify_hashes(ROOT/'build',final['installed'])
    build=cpu.read(BASE/'build-01/result.json')
    cpu.require(build['passed'] and build['zero_warnings'],'build gate')
    s.cpu_gate()
    cpu.require(cpu.read(BASE/'driver-cpu-final/result.json')['passed'],'driver gate')
    b=OUTPUT/'business';result=cpu.read(b/'result.json')
    cpu.require(result['status']=='PASS_BOUNDED_OPERATIONS_LISTENER' and result['successes']==5 and
        result['real_tools']==2 and result['cancelled']==1 and result['negative_probes']==6,'business totals')
    cpu.require(result['counts']['starts']==1 and result['counts']['generation_attempts']==6 and
        all(result['counts'][k]<=plan['limits'][k] for k in result['counts']),'budget')
    cpu.require('41 tests, 41 passed, 0 failed, 0 skipped' in (b/'native/stdout.log').read_text(),'native cases')
    probes=cpu.read(b/'startup-probes.json');cpu.require(probes['passed'] and len(probes['probes'])==5,'startup probes')
    for row in probes['probes']:
        folder=b/row['label'];wait=cpu.read(folder/'waitable.json')
        s.verify_probe(row['label'],(folder/'stderr.log').read_text(),wait['returncode'],row['public_port'],row['operations_port'])
        cpu.require(row['ports_released'] and row['returncode']==wait['returncode'],'probe cleanup/exit')
    successes=[]
    for label in ('ordinary-recovery','project-alpha-read','project-alpha-answer','project-beta-read','project-beta-answer'):
        prefix=b/label
        parsed=chat_client.read_sse(io.BytesIO(prefix.with_name(label+'-wire.bin').read_bytes()))
        record=cpu.read(prefix.with_name(label+'-response.json'));seconds=record.pop('elapsed')
        request=cpu.read(prefix.with_name(label+'-request.json'))['body']
        cpu.require(asdict(parsed)==record and parsed.id==request['request_id'] and
            parsed.usage['completion_tokens']<=request['max_tokens']<= (64 if 'beta' in label else 512),'wire/identity/budget')
        if label=='ordinary-recovery':cpu.require(parsed.message['content'].strip()=='710003','recovery quality')
        if label.endswith('-answer'):
            tool=json.loads(cpu.read(b/(label.replace('-answer','-tool-result')+'.json'))['content']);source=tool['source']
            cpu.require(tool['ok'] and source['path']=='docs/MODEL.md' and source['sha256']==cpu.sha(ROOT/'docs/MODEL.md') and
                        source['start_line']==1 and source['end_line']==24,'real source provenance')
            s.s.a.validate_answer(parsed.message['content'],{source['citation']},1)
            cpu.require('Qwen4ExpForConditionalGeneration' in parsed.message['content'],'real answer')
        successes.append(dict(label=label,seconds=seconds,usage=parsed.usage,finish_reason=parsed.finish_reason))
    cancel=cpu.read(b/'cancel-active-request.json')['body']
    s.s.cancelled_wire((b/'cancel-active-wire.bin').read_bytes(),cancel['request_id'])
    s.previous.saturation_evidence(cpu.read(b/'saturated-metrics.json'))
    cpu.require(cpu.read(b/'saturated-diagnostic.json')['state']=='HEALTHY' and cpu.read(b/'saturated-supervisor.json')['ready'],'operations health')
    metrics=cpu.read(b/'final-metrics.json')
    for key in ('q4t_active_chats','q4t_business_connections','q4t_public_connections','q4t_request_body_bytes',
                'q4t_structured_workers_active','q4t_structured_active','q4t_structured_preparing'):
        cpu.require(metrics[key]==0,'resource retained: '+key)
    cpu.require(metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4,'grammar cleanup')
    events=[json.loads(line) for line in (OUTPUT/'lease/events.jsonl').read_text().splitlines()]
    observations=[r for r in events if r['event']=='monitor']
    cpu.require(events[0]['event']=='lease_acquired' and events[-1]['event']=='lease_released' and
        cpu.read(OUTPUT/'lease/exit.json')['status']=='COMPLETED' and observations and
        all(not r['conflicts'] and r['unreadable_processes']==0 for r in observations),'guard evidence')
    duration=events[-1]['monotonic']-events[0]['monotonic']
    cpu.require(0<duration<300 and result['seconds']<300,'duration')
    post=cpu.read(BASE/'postflight/result.json');cpu.require(post['passed'] and len(post['owned'])==8,'postflight/ownership')
    pkg=cpu.read(BASE/'accepted-package.json')
    manifest=s.pkg.verify(pkg['package']['package'],pkg['package']['manifest_sha256'])
    cpu.require(manifest['version']==6 and not pkg['managed_lifecycle_accepted'],'package scope')
    return dict(status='ACCEPT_BOUNDED_STARTUP_LISTENERS',installed=final['installed'],
        protocol_sha256=plan['protocol'],source_commit=plan['source_commit'],native_tests=41,
        cli_no_model_probes=probes['probes'],models=1,generation_attempts=6,successful_responses=5,
        real_tools=2,cancelled=1,negative_http_probes=6,successes=successes,
        lease_seconds=duration,lease_observations=len(observations),resources=post,
        rollback=str(PACKAGE/'previous'),previous_binary_sha256=plan['previous_binary_sha256'],
        runtime_sources=len(cpu.read(PACKAGE/'candidate/runtime-source.json')),
        numerical_reuse=cpu.read(PACKAGE/'numerical-reuse.json'),
        declarations_verified=True,accepted_package=pkg['package'],managed_lifecycle_accepted=False,
        long_runs=False,performance_acceptance=False,actual_lan_accepted=False,
        startup_tcp_backlog=128,startup_http_available=False,physical_ram_hard_limit=False)


def private_values():
    plan=cpu.read(PACKAGE/'plan.json');front=cpu.read(plan['frontend']);policy=cpu.read(plan['policy'])
    values={policy['gateway_token'],*(c['api_key'] for c in front['clients'])}
    for path in (OUTPUT/'business').glob('*-request.json'):
        body=cpu.read(path).get('body') or {}
        if body.get('cancel_token'):values.add(body['cancel_token'])
    return [v.encode() for v in values]+[b'-----BEGIN PRIVATE KEY-----',b'-----BEGIN RSA PRIVATE KEY-----']


def files():
    values=[]
    for path in BASE.rglob('*'):
        rel=path.relative_to(BASE)
        if path.is_file() and rel.parts[0] not in ('engine','host') and 'source' not in rel.parts:
            # Caller logs must never be archived while this collector writes.
            if path.name in ('archive.stdout','archive.stderr','verify.stdout','verify.stderr'):continue
            values.append(path)
    return sorted(values)


def archive():
    result=audit();secrets=private_values();records=[];payload={}
    for path in files():
        rel=path.relative_to(BASE);row=dict(path=str(path),sha256=cpu.sha(path),bytes=path.stat().st_size)
        eligible=(rel.parts[0]!='private' and not path.name.endswith('-request.json') and
            path.suffix in ('.json','.jsonl','.stdout','.stderr','.log','.txt','.bin','.yml','.h') and
            row['bytes']<=2*1024**2)
        if eligible:
            raw=path.read_bytes()
            if not any(secret in raw for secret in secrets):
                name='records/'+str(rel);payload[name]=raw;row['artifact']=name
        records.append(row)
    rawtar=io.BytesIO()
    with tarfile.open(fileobj=rawtar,mode='w') as tar:
        for name,raw in sorted(payload.items()):
            info=tarfile.TarInfo(name);info.size=len(raw);info.mode=0o600
            tar.addfile(info,io.BytesIO(raw))
    zipped=gzip.compress(rawtar.getvalue(),mtime=0)
    ARCHIVE.mkdir(parents=True)
    (ARCHIVE/'records.tar.gz').write_bytes(zipped)
    result.update(archived_records=len(payload),local_references=len(records),
        privacy='Raw secret-bearing requests/keys and ELF stay local as hash references; archive is not a disaster-recovery backup.')
    cpu.save(ARCHIVE/'result.json',result)
    cpu.save(ARCHIVE/'manifest.json',dict(version=1,records=records,result_sha256=cpu.sha(ARCHIVE/'result.json'),
        archive_sha256=cpu.sha(ARCHIVE/'records.tar.gz'),archive_bytes=len(zipped),
        verifier=dict(path=str(Path(__file__).resolve()),sha256=cpu.sha(__file__))))
    verify()


def verify():
    manifest=cpu.read(ARCHIVE/'manifest.json')
    cpu.require(cpu.sha(manifest['verifier']['path'])==manifest['verifier']['sha256'],'verifier changed')
    cpu.require(cpu.sha(ARCHIVE/'result.json')==manifest['result_sha256'] and
        cpu.sha(ARCHIVE/'records.tar.gz')==manifest['archive_sha256'],'archive changed')
    with tarfile.open(ARCHIVE/'records.tar.gz','r:gz') as tar:
        payload={row.name:tar.extractfile(row).read() for row in tar.getmembers()}
    cpu.require(set(payload)=={r['artifact'] for r in manifest['records'] if 'artifact' in r},'inventory')
    secrets=private_values()
    for row in manifest['records']:
        cpu.require(cpu.sha(row['path'])==row['sha256'],'local reference changed: '+row['path'])
        if 'artifact' in row:
            raw=payload[row['artifact']]
            cpu.require(len(raw)==row['bytes'] and hashlib.sha256(raw).hexdigest()==row['sha256'] and
                        not any(secret in raw for secret in secrets),'raw artifact/secret mismatch')
    result=cpu.read(ARCHIVE/'result.json');rebuilt=audit()
    cpu.require(all(result[k]==v for k,v in rebuilt.items()),'reconstructed result differs')
    print(json.dumps(dict(verified=True,records=result['archived_records'],references=len(manifest['records']),
        lease_seconds=result['lease_seconds'],installed=result['installed'],archive_sha256=manifest['archive_sha256'])))


if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('preflight','postflight','package','archive','verify'))
    action=p.parse_args().action
    if action in ('preflight','postflight'):resources(action)
    else:globals()[action]()
