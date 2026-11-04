"""Reparse preserved TLS/model evidence and archive without API keys/private keys."""
import argparse
from dataclasses import asdict
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_gateway import correction as c, gateway as g, acceptance as m
from tools.serve_service import package as pkg, supervisor as sup
from tools.serve_soak.evidence import guard_archive
from tools.serve_handoff.stage import h

BASE=c.BASE
DEST=ROOT/'docs/evidence/serve-gateway-20261104'


def response(folder,label):
    body=pkg.read(folder/(label+'-request.json'))
    receipt=pkg.read(folder/(label+'-http.json'))
    g.require(receipt['eof'] and receipt['status']==200 and
        receipt['headers']['X-Request-Id']==body['request_id'] and
        receipt['headers']['X-Q4T-Gateway-Request-Id'],'incomplete HTTP identity')
    raw=(folder/(label+'-wire.bin')).read_bytes()
    value=(m.a.read_sse if body['stream'] else m.a.read_json)(io.BytesIO(raw))
    g.require(value.id==body['request_id'] and value.model==body['model'] and
        asdict(value)==pkg.read(folder/(label+'-response.json')),'response differs from wire')
    return value,body


def check():
    c.configure();plan=pkg.read(c.PLAN)
    # Historical proof binds bytes; a fixture certificate expiring later does
    # not invalidate the past TLS handshake. Only actual run/check revalidates
    # current certificate lifetime through the production gateway entry.
    g.require(plan['limits']==m.LIMITS and plan['gpu_window_seconds']==m.GPU_WINDOW,'budget differs')
    for name,digest in plan['source_files'].items():g.require(g.sha(ROOT/name)==digest,'source changed: '+name)
    for path,key in ((c.CPU/'result.json','corrective_cpu_sha256'),
                     (BASE/'correction-reuse.json','reuse_sha256')):
        g.require(g.sha(path)==plan[key],'acceptance identity differs')
    g.require(c.reuse().items() <= pkg.read(BASE/'correction-reuse.json').items(),'reuse evidence changed')
    pkg.verify(plan['package']['package'],plan['package']['manifest_sha256'])
    directory=Path(plan['gateway']['package'])
    g.require(g.sha(directory/'manifest.json')==plan['gateway']['manifest_sha256'],'gateway package differs')
    gateway=pkg.read(directory/'manifest.json')
    g.require(g.sha(gateway['nginx']['path'])==gateway['nginx']['sha256'] and
        gateway['generator_sha256']==g.sha(g.__file__) and
        gateway['api_contract_sha256']==g.sha(g.API_CONTRACT),'gateway dependency differs')
    for name,digest in gateway['files'].items():g.require(g.sha(directory/name)==digest,'private input changed')
    g.require(g.sha(BASE/'model-private/policy-private.json')==plan['private_policy_sha256'] and
        g.sha(ROOT/'build/q4t')==plan['package']['binary_sha256'] and
        g.sha(ROOT/'build/q4t.release.json')==plan['default_release_sha256'],'default or keys changed')
    g.require(pkg.read(c.CPU/'result.json')['passed'],'corrective CPU failed')
    old,new=BASE/'model',c.OUT
    first,last=pkg.read(old/'result.json'),pkg.read(new/'result.json')
    g.require(first['status']=='FAILED' and first['cleanup_confirmed'] and
        first['counts']==dict(generations=7,negatives=2,controls=6),'original failure changed')
    g.require(last['status']=='PASS_TLS_MODEL_TOOL_CANCEL' and last['cleanup_confirmed'] and
        last['counts']['generations']==3 and last['counts']['negatives']==2 and
        last['counts']['controls']<=96 and last['gpu_window_elapsed']<300 and
        last['elapsed_seconds']<360,'corrective acceptance incomplete')
    ids,tokens=set(),set()
    for folder,labels in ((old,('ordinary-alpha','ordinary-beta','strict','project-read','project-answer')),
                         (new,('queued-beta','after-cancel-recovery'))):
        for label in labels:
            value,body=response(folder,label)
            g.require(body['request_id'] not in ids and body['cancel_token'] not in tokens,'credentials reused')
            ids.add(body['request_id']);tokens.add(body['cancel_token'])
            if label in ('ordinary-alpha','ordinary-beta','queued-beta','after-cancel-recovery'):
                g.require(value.finish_reason=='stop' and value.message['content'].strip()=='710003','ordinary quality')
            if label=='strict':
                g.require(value.finish_reason=='stop' and json.loads(value.message['content'])=={'ok':True},'strict quality')
    started=pkg.read(old/'project-tool-started.json')
    tool=pkg.read(old/'project-tool-result.json');source=json.loads(tool['content'])['source']
    g.require(started['arguments']==dict(path='docs/MODEL.md',start_line=1,line_count=24) and
        tool['tool_call_id']==started['call_id'] and source['sha256']==g.sha(ROOT/'docs/MODEL.md'),
        'real tool provenance mismatch')
    expected='\n'.join(f'{i+1}: {line}' for i,line in enumerate((ROOT/'docs/MODEL.md').read_text().splitlines()[:24]))
    g.require(source['text']==expected,'tool did not return the real document lines')
    answer=pkg.read(old/'project-answer-response.json')['message']['content']
    m.a.validate_answer(answer,{source['citation']},1)
    g.require('Qwen4ExpForConditionalGeneration' in answer,'project answer quality')
    m.a.healthy_idle(pkg.read(old/'before-cancel.json')['metrics'],5)
    before=pkg.read(old/'before-cancel.json')['metrics']
    g.require(before['q4t_structured_workers_started_total']==before['q4t_structured_workers_reaped_total']==3,
              'original grammar did not recycle')
    active=pkg.read(new/'active-cancel-request.json')
    cancel=m.cancelled_wire((new/'active-cancel-wire.bin').read_bytes(),active['request_id'])
    g.require(pkg.read(new/'cancel-verified.json')==cancel and
        pkg.read(new/'explicit-cancel-http.json')['status']==202 and
        json.loads((new/'explicit-cancel-wire.bin').read_bytes())=={'cancel_requested':True},'cancel evidence mismatch')
    for folder,label,status,code in ((old,'unauthorized',401,'gateway_unauthorized'),
        (old,'alpha-quota',429,'gateway_rate_limit'),(new,'alpha-quota',429,'gateway_rate_limit'),
        (new,'old-route-refused',404,'gateway_route_not_found'),(old,'explicit-cancel',404,'not_found')):
        receipt=pkg.read(folder/(label+'-http.json'))
        g.require(receipt['eof'] and receipt['status']==status and
            json.loads((folder/(label+'-wire.bin')).read_bytes())['error']['code']==code,'error classification changed')
    original_active=(old/'active-cancel-wire.bin').read_bytes()
    g.require(original_active.count(b'"code":"server_shutdown"')==1 and
        original_active.count(b'data: [DONE]')==1 and
        original_active.rstrip().endswith(b'data: [DONE]') and
        json.loads((old/'queued-beta-wire.bin').read_bytes())['error']['code']=='server_shutdown',
        'original interrupted requests misrepresented')
    metrics=pkg.read(new/'final-metrics.json')['metrics']
    g.require(all(metrics[n]==0 for n in m.a.EMPTY) and metrics['q4t_seq_slots_free']==1 and
        metrics['q4t_requests_total']==3 and metrics['q4t_requests_success_total']==2 and
        metrics['q4t_requests_aborted_total']==1 and metrics['q4t_requests_error_total']==0 and
        metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==0,
        'corrective final resources/accounting differs')
    guards={}
    for name in ('model','model-corrected'):
        folder=BASE/name
        terminal=pkg.read(folder/'managed-run/terminal.json')
        g.require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and
            terminal['worker']['attempts']==1 and terminal['worker']['restarts']==0,'lifecycle incomplete')
        guards[name]=guard_archive(folder/'managed-run/lease/events.jsonl',terminal['guard'])
        gpu=pkg.read(folder/'gpu-final.json')
        g.require(gpu['status']=='OK' and gpu['pids']==[],'historical GPU cleanup missing')
    transport=pkg.read(BASE/'transport/result.json')
    g.require(transport['status']=='PASS_BOUNDED_TLS_TRANSPORT' and
        transport['http_attempts']==35 and transport['cleanup_confirmed'] and
        pkg.read(BASE/'transport-lease/exit.json')['status']=='COMPLETED','TLS fixture incomplete')
    for row in pkg.read(BASE/'transport/upstream.json'):
        g.require(not any(row[k] for k in ('authorization_seen','cookie_seen','forwarded_seen','client_id_seen')),
                  'identity headers reached upstream')
    for path in (BASE/'transport').glob('*.json'):
        row=pkg.read(path)
        if isinstance(row,dict) and 'expected' in row:
            g.require(row['status']==row['expected'],'transport response differs')
    g.require(not (ROOT/'.q4t-work/serve-soak-20261103/day/execution').exists(),'user day unexpectedly started')
    return dict(passed=True,scope='loopback TLS with two clients; no actual LAN deployment',
        reused_successful_requests=5,corrective_successful_requests=2,
        real_tool_executions=1,explicit_cancel=1,original_shutdown_requests=2,
        original_model_failure_retained=True,model_starts=2,total_model_attempts=10,
        original_controls=6,independent_stop_controls=1,corrective_controls=last['counts']['controls'],
        gateway_rejections=4,guards=guards,transport_attempts=35,
        fixture_cancel_was_not_runner_proof=True,
        gpu_window_seconds=first['gpu_window_elapsed']+last['gpu_window_elapsed'],
        performance_acceptance=False,day_started=False,new_model_or_http=0,
        package_manifest_sha256=plan['gateway']['manifest_sha256'])


def secrets_in_phase():
    values=[]
    folders=(BASE,ROOT/'build/serve-gateway-tests')
    for folder in folders:
        for path in folder.rglob('policy*.json'):
            value=pkg.read(path)
            values.extend(x['api_key'].encode() for x in value.get('clients',[]))
    return values


def private_file(path):
    return (path.name in ('key.pem','private-key.pem','nginx.conf','policy.json') or
            path.name.startswith('policy-private') or path.name=='syntax-private.log' or
            path.parent.name=='runtime')


def postflight():
    unit='soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'
    mask=Path.home()/'.config/systemd/user'/unit
    g.require(mask.is_symlink() and os.readlink(mask)=='/dev/null','SOAR persistent mask absent')
    state=subprocess.check_output(['systemctl','--user','show',unit,'-p','MainPID',
        '-p','ActiveState','-p','UnitFileState'],text=True,timeout=10)
    g.require(all(x in state for x in ('MainPID=0','ActiveState=inactive','UnitFileState=masked')),'SOAR running')
    containers=subprocess.check_output(['sudo','-n','docker','ps','--format','{{.ID}} {{.Names}}'],
                                       text=True,timeout=10)
    g.require(not containers.strip(),'container running; inspect before handoff')
    lock=m.a.guard.common_lock()
    g.require(m.a.lock_available(lock) and lock.stat().st_ino==3294067 and
        not os.path.lexists(lock.with_name(lock.name+'.recovery-required')),'GPU lock unavailable/replaced')
    for folder in (BASE/'model',c.OUT):
        owners=pkg.read(folder/'owners.json')+[pkg.read(folder/'gateway-owner.json')]
        for owner in owners:
            try:current=sup.process_identity(owner['pid'])
            except FileNotFoundError:continue
            g.require(current!=owner,'owned process still alive')
    m.a.require_idle(BASE/'postflight-gpu.json')
    result=subprocess.run([sys.executable,'-B','tools/serve_soak/stage.py','check','--profile','day'],
        capture_output=True,text=True,timeout=20)
    g.require(result.returncode==0,'user day plan identity changed')
    h.save(BASE/'day-check.json',dict(returncode=result.returncode,stdout=result.stdout,
                                   stderr=result.stderr,new_model_or_http=0))
    return dict(passed=True,soar_state=state,containers=containers,lock_inode=lock.stat().st_ino,
        lock_available=True,owned_processes_gone=True,day_prepared_not_started=True,
        default_binary_sha256=g.sha(ROOT/'build/q4t'),
        default_sidecar_sha256=g.sha(ROOT/'build/q4t.release.json'))


def verify_archive():
    h.verify_seal(DEST)
    manifest=pkg.read(DEST/'manifest.json')
    expected={r['member']:r for r in manifest['records']}
    seen=set();secrets=secrets_in_phase()
    with tarfile.open(DEST/'raw-records.tar.gz','r:gz') as archive:
        for member in archive:
            g.require(member.isfile() and member.name in expected and member.name not in seen,'unexpected member')
            raw=archive.extractfile(member).read();row=expected[member.name]
            g.require(len(raw)==row['bytes'] and hashlib.sha256(raw).hexdigest()==row['sha256'],'archive differs')
            g.require(not any(s in raw for s in secrets) and b'-----BEGIN PRIVATE KEY' not in raw,
                      'private credentials in archive')
            h.check_identity(row);seen.add(member.name)
    g.require(seen==expected.keys(),'incomplete archive')
    for row in manifest['local_artifacts']:h.check_identity(row)
    for name,row in manifest['aliases'].items():
        h.check_identity(row);g.require(g.sha(DEST/name)==row['sha256'],'alias changed')
    return dict(check(),records=len(seen),local_artifacts=len(manifest['local_artifacts']),
                no_api_keys_or_private_keys_archived=True)


def archive():
    result=check()
    g.require(not DEST.exists(),'preserve prior archive')
    if (BASE/'verified.json').exists():
        g.require(pkg.read(BASE/'verified.json')==result,'preserved verification differs')
    else:h.save(BASE/'verified.json',result)
    h.save(BASE/'postflight.json',postflight())
    # Recover original source bytes from the frozen Git commit, verifying each
    # SHA against the pre-execution plan; these are not invented run records.
    original=pkg.read(BASE/'model-plan.json')
    for name,digest in original['source_files'].items():
        raw=subprocess.check_output(['git','show',original['source_commit']+':'+name])
        g.require(hashlib.sha256(raw).hexdigest()==digest,'original source differs')
        dest=BASE/'original-model-source'/name;dest.parent.mkdir(parents=True,exist_ok=True)
        g.save(dest,raw)
    DEST.mkdir()
    records,local=[],[]
    secrets=secrets_in_phase()
    paths=[(str(p.relative_to(BASE)),p) for p in sorted(BASE.rglob('*')) if p.is_file()]
    fixtures=ROOT/'build/serve-gateway-tests'
    paths.extend(('cpu-private-fixtures/'+str(p.relative_to(fixtures)),p)
                 for p in sorted(fixtures.rglob('*')) if p.is_file())
    with (DEST/'raw-records.tar.gz').open('xb') as output:
        with gzip.GzipFile(fileobj=output,mode='wb',mtime=0) as zipped:
            with tarfile.open(fileobj=zipped,mode='w|') as archive:
                for member,path in paths:
                    g.require(not path.is_symlink(),'unexpected evidence symlink')
                    if 'vendor/root/' in member:
                        if path.name=='nginx':local.append(h.identity(path))
                        continue
                    if private_file(path) or path.suffix=='.deb':
                        local.append(h.identity(path));continue
                    raw=path.read_bytes()
                    g.require(not any(s in raw for s in secrets) and b'-----BEGIN PRIVATE KEY' not in raw,
                              'secret found in proposed archive: '+member)
                    entry=tarfile.TarInfo(member)
                    entry.size,entry.mode,entry.mtime=len(raw),0o600,0
                    archive.addfile(entry,io.BytesIO(raw))
                    records.append(dict(h.identity(path),member=member))
    backend=Path(original['package']['package'])
    local.extend(h.identity(backend/n) for n in ('q4t','manifest.json'))
    aliases={name+'.json':h.identity(path) for name,path in dict(
        original_plan=BASE/'model-plan.json',corrective_plan=c.PLAN,
        original_result=BASE/'model/result.json',corrective_result=c.OUT/'result.json',
        verified=BASE/'verified.json',postflight=BASE/'postflight.json').items()}
    for name,row in aliases.items():h.copy_checked(row,DEST/name)
    h.save(DEST/'manifest.json',dict(records=records,local_artifacts=local,aliases=aliases,
        archiver=h.identity(__file__),scope=result['scope'],
        private_material='Local hashes only for policies, nginx configs, private keys and syntax diagnostics.',
        evidence_limitation='Early CPU TemporaryDirectory teardown deleted three original nginx diagnostic '
        'fixture directories. Test failures and source bytes remain; syntax-diagnosis is a separate '
        'reproduction, not the missing originals. All later private fixtures are retained.',
        long_validation_owner='user',day_started=False))
    h.seal(DEST)
    return verify_archive()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('check','archive','verify-archive'))
    args=parser.parse_args()
    print(json.dumps(check() if args.action=='check' else
                     archive() if args.action=='archive' else verify_archive()))
