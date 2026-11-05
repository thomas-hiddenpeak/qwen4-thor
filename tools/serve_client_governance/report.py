"""Offline resource audit and lossless secret-screened client governance receipts."""
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
from tools.serve_client_governance import stage as s,cpu
from tools.examples import chat_client

BASE,OUTPUT,PACKAGE=s.BASE,s.OUTPUT,s.PACKAGE
ARCHIVE=ROOT/'docs/evidence/serve-client-governance-20261105'
UNIT='soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'


def resources(label):
    out=BASE/label;out.mkdir()
    s.a.require_idle_gpu(out/'gpu.json')
    lock=s.s.a.guard.common_lock();st=lock.stat()
    cpu.require((st.st_dev,st.st_ino)==(66305,3294067) and s.s.a.lock_available(lock),
                'original shared GPU lock not available')
    for port in (s.PORT,s.TLS_PORT):
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
    cpu.save(out/'result.json',dict(passed=True,owned=owners,ports=[s.PORT,s.TLS_PORT],
        gpu_empty=True,lock=dict(dev=st.st_dev,inode=st.st_ino,available=True),
        restore_unit_masked=True,containers_empty=True,long_runs=False))


def audit():
    plan=s.check();s.h.verify_seal(OUTPUT)
    final=cpu.read(OUTPUT/'finalized.json')
    cpu.require(final['status']=='DELIVERED','delivery')
    s.a.verify_hashes(ROOT/'build',final['installed'])
    build=cpu.read(BASE/s.BUILD_LABEL/'result.json')
    gate=cpu.read(BASE/s.CPU_LABEL/'result.json')
    cpu.require(build['passed'] and build['zero_warnings'] and gate['passed'],'build/CPU gate')
    b=OUTPUT/'business';result=cpu.read(b/'result.json')
    cpu.require(result['status']=='PASS_BOUNDED_CLIENT_GOVERNANCE' and
        result['successes']==12 and result['cancelled']==2 and result['real_tools']==2,'business totals')
    cpu.require(result['counts']['starts']==2 and result['counts']['generation_attempts']==16 and
        all(result['counts'][k]<=plan['limits'][k] for k in result['counts']),'budget')
    successes=[]
    for path in sorted(b.glob('*-response.json')):
        label=path.name[:-len('-response.json')]
        req=cpu.read(b/(label+'-request.json'))['body']
        raw=(b/(label+'-wire.bin')).read_bytes()
        parsed=(chat_client.read_sse if req['stream'] else chat_client.read_json)(io.BytesIO(raw))
        record=cpu.read(path);elapsed=record.pop('elapsed',None)
        cpu.require(asdict(parsed)==record and parsed.id==req['request_id'] and
            parsed.usage['completion_tokens']<=req['max_tokens'],'raw response/usage')
        if not label.startswith('project-'):
            cpu.require(parsed.finish_reason=='stop' and parsed.message['content'].strip()=='710003','echo quality')
        successes.append(dict(label=label,usage=parsed.usage,body_read_seconds=elapsed))
    cpu.require(len(successes)==12,'wire response count')
    for name in ('alpha','beta'):
        label='project-'+name
        call_response=chat_client.read_sse(io.BytesIO((b/(label+'-read-wire.bin')).read_bytes()))
        calls=s.s.a.prepare_tool_calls(call_response,s.s.a.VALIDATORS)
        cpu.require(len(calls)==1 and calls[0].arguments==dict(path='docs/MODEL.md',start_line=1,line_count=24),'real tool input')
        actual=s.s.a.ProjectRecords(ROOT).execute(calls[0])
        actual['application_budget']=dict(remaining_model_requests=1,remaining_tool_calls=0,final_response_required=True)
        receipt=cpu.read(b/(label+'-tool-result.json'))
        cpu.require(json.loads(receipt['content'])==actual and receipt['tool_call_id']==calls[0].call_id,'real tool receipt')
        answer=cpu.read(b/(label+'-answer-response.json'))
        s.s.a.validate_answer(answer['message']['content'],{actual['source']['citation']},1)
        cpu.require(answer['finish_reason']=='stop' and 'Qwen4ExpForConditionalGeneration' in answer['message']['content'],'project quality')
    for label in ('active','a3'):
        body=cpu.read(b/(label+'-request.json'))['body']
        s.s.cancelled_wire((b/(label+'-wire.bin')).read_bytes(),body['request_id'])
    for label,code in (('rpm-denied','client_rpm_exhausted'),('tpm-denied','client_tpm_exhausted')):
        cpu.require(cpu.read(b/(label+'-http.json'))['status']==429 and
            cpu.read(b/(label+'-wire.bin'))['error']['code']==code,'pre-SSE refusal')
    log=(b/'scoped-model/stderr.log').read_text()
    order=s.admitted(log)
    expected=[cpu.read(b/(label+'-request.json'))['body']['request_id'] for label in ('active','b1','a1','a2')]
    # Two tool loops precede the fair batch; recovery follows it.
    cpu.require(order[4:8]==expected and cpu.read(b/'a3-request.json')['body']['request_id'] not in order,'admission order')
    fairness=cpu.read(b/'fairness.json')
    before=s.s.parse_metrics((b/'before-queued-cancel-wire.bin').read_text())
    after_paths=list(b.glob('after-queued-cancel-*-wire.bin'))
    after=s.s.parse_metrics(max(after_paths,key=lambda p:int(p.name.split('-')[-3])).read_text())
    cost=cpu.read(b/'a3-preflight-wire.bin')
    reserved=cost['prompt_tokens']+cost['output']['available_tokens']
    cpu.require(s.rate(before,'alpha','reserved_tokens')-s.rate(after,'alpha','reserved_tokens')==reserved and
        s.rate(before,'alpha','settled_tokens_total')==s.rate(after,'alpha','settled_tokens_total'),'queued refund')
    quotas=cpu.read(b/'quotas.json');values=quotas['final']
    raw_final=max(b.glob('quota-final-*-wire.bin'),key=lambda p:int(p.name.split('-')[-3]))
    cpu.require(values==s.s.parse_metrics(raw_final.read_text()),'final raw metrics')
    cpu.require(sum(s.rate(values,c,'settled_tokens_total') for c in ('alpha','beta'))==
        values['q4t_prompt_tokens_total']+values['q4t_generation_tokens_total'],'logical settlement ledger')
    for c in ('alpha','beta'):
        cpu.require(s.rate(values,c,'reserved_tokens')==0 and s.rate(values,c,'requests')==1,'minute accounting')
    cpu.require(values['q4t_requests_success_total']==10 and values['q4t_requests_aborted_total']==2 and
        values['q4t_structured_workers_started_total']==values['q4t_structured_workers_reaped_total']==4,'model outcome/grammar')
    for key in s.s.a.EMPTY:
        cpu.require(values[key]==0,'idle resource: '+key)
    events=[json.loads(line) for line in (OUTPUT/'lease/events.jsonl').read_text().splitlines()]
    observations=[r for r in events if r['event']=='monitor']
    cpu.require(events[0]['event']=='lease_acquired' and events[-1]['event']=='lease_released' and
        cpu.read(OUTPUT/'lease/exit.json')['status']=='COMPLETED' and observations and
        all(not r['conflicts'] and r['unreadable_processes']==0 for r in observations),'guard')
    duration=events[-1]['monotonic']-events[0]['monotonic']
    cpu.require(0<duration<480 and result['seconds']<480,'GPU duration')
    post=cpu.read(BASE/'postflight/result.json')
    cpu.require(post['passed'] and len(post['owned'])==3,'owned process cleanup')
    s.pkg.verify_declarations(cpu.read(ROOT/'build/q4t.release.json'),required=True)
    import re
    latencies=[float(n) for n in re.findall(r'event=end [^\n]* elapsed_ms=([0-9.]+)',log)]
    return dict(status='ACCEPT_BOUNDED_CLIENT_GOVERNANCE',installed=final['installed'],
        protocol_sha256=plan['protocol'],source_commit=plan['source_commit'],models=2,
        generation_attempts=16,successful_responses=12,real_tools=2,cancelled=2,refusals=2,
        successes=successes,handler_elapsed_ms=latencies,lease_seconds=duration,
        lease_observations=len(observations),resources=post,rollback=str(PACKAGE/'previous'),
        previous_binary_sha256=plan['previous_binary_sha256'],cpu_checks=gate['checks'],
        numerical_reuse=cpu.read(PACKAGE/'numerical-reuse.json'),declarations_verified=True,
        performance_acceptance=False,long_runs=False,actual_lan_accepted=False,
        managed_lifecycle_accepted=False,physical_ram_hard_limit=False)


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
        eligible=(not rel.parts[0].startswith('private') and not path.name.endswith('-request.json') and
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
    p.add_argument('action',choices=('preflight','postflight','archive','verify'))
    action=p.parse_args().action
    if action in ('preflight','postflight'):resources(action)
    else:globals()[action]()
