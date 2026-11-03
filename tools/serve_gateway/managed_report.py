"""Offline lifecycle/wire verification and credential-free evidence archive."""
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
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_gateway import managed_stage as s,managed as m
from tools.serve_gateway.report import private_file
from tools.serve_soak.evidence import guard_archive
from tools.serve_handoff.stage import h

BASE=s.BASE;OUT=s.OUT
DEST=ROOT/'docs/evidence/serve-gateway-managed-20261104'


def check():
    s.configure();plan=m.pkg.read(s.PLAN)
    for name,digest in plan['source_files'].items():m.g.require(m.pkg.sha(ROOT/name)==digest,'source drift: '+name)
    m.g.require(plan['limits']==s.LIMITS and plan['gpu_window_seconds']==s.GPU_WINDOW,'budget drift')
    m.g.require(m.pkg.sha(BASE/'cpu-corrected/result.json')==plan['cpu_sha256'],'CPU record drift')
    for path in (BASE/'cpu-corrected/result.json',BASE/'driver-cpu/result.json'):
        value=m.pkg.read(path);m.g.require(value['passed'],'CPU failed')
        for name,digest in value['sources'].items():m.g.require(m.pkg.sha(ROOT/name)==digest,'tested source drift')
    m.pkg.verify(plan['package']['package'],plan['package']['manifest_sha256'])
    # Historical proof validates immutable bytes, not current certificate TTL.
    for descriptor in plan['gateways']:
        package=Path(descriptor['package'])
        m.g.require(m.pkg.sha(package/'manifest.json')==descriptor['manifest_sha256'],'package identity changed')
        manifest=m.read(package/'manifest.json')
        for name,digest in manifest['sources'].items():
            m.g.require(m.pkg.sha(package/'source'/name)==digest,'package source changed')
        gateway=m.read(package/'gateway/manifest.json')
        m.g.require(m.pkg.sha(package/'gateway/manifest.json')==manifest['gateway']['manifest_sha256'] and
            m.pkg.sha(package/'nginx')==gateway['nginx']['sha256'],'gateway input changed')
        for name,digest in gateway['files'].items():
            m.g.require(m.pkg.sha(package/'gateway'/name)==digest,'gateway private file changed')
    m.g.require(m.pkg.sha(ROOT/'build/q4t')==plan['package']['binary_sha256'] and
        m.pkg.sha(ROOT/'build/q4t.release.json')==plan['default_release_sha256'],'default pair changed')
    result=m.pkg.read(OUT/'result.json')
    m.g.require(result['status']=='PASS_MANAGED_TLS_GATEWAY_LIFECYCLE' and result['cleanup_confirmed'] and
        result['counts']['generations']==8 and result['counts']['negatives']==0 and
        result['counts']['controls']<=128 and result['gpu_window_elapsed']<360 and
        result['elapsed_seconds']<420,'run result incomplete')
    ids=set();log=(OUT/'managed-run/backend.log').read_text()
    successful=('ordinary','strict','project-read','project-answer','graceful-stream','final-recovery')
    for label in (*successful,'front-2-interrupted','front-3-interrupted'):
        body=m.pkg.read(OUT/(label+'-request.json'))
        raw=(OUT/(label+'-wire.bin')).read_bytes();http=m.pkg.read(OUT/(label+'-http.json'))
        m.g.require(http['status']==200 and http['headers']['X-Request-Id']==body['request_id'] and
            http['headers']['X-Q4T-Gateway-Request-Id'] and body['request_id'] not in ids,'wire identity mismatch')
        ids.add(body['request_id'])
        if label in successful:
            parsed=(s.a.read_sse if body['stream'] else s.a.read_json)(io.BytesIO(raw))
            m.g.require(http['eof'] and asdict(parsed)==m.pkg.read(OUT/(label+'-response.json')) and
                parsed.id==body['request_id'],'response differs from complete wire')
            if label in ('ordinary','final-recovery'):
                m.g.require(parsed.finish_reason=='stop' and parsed.message['content'].strip()=='710003','ordinary quality')
            if label=='strict':m.g.require(json.loads(parsed.message['content'])=={'ok':True},'strict quality')
            if label=='graceful-stream':m.g.require(parsed.finish_reason=='stop' and
                parsed.message['content'].strip().splitlines()==[str(n) for n in range(1,41)],'drained stream quality')
        else:
            m.g.require(s.incomplete(raw,body['request_id'])==m.pkg.read(OUT/(label+'-verified.json')),
                        'interrupted wire classification changed')
        ends=[line for line in log.splitlines() if 'event=end ' in line and f'id={body["request_id"]} ' in line]
        reason='completed' if label in successful else 'client_disconnected'
        m.g.require(len(ends)==1 and 'released=1' in ends[0] and 'reason='+reason in ends[0],'trace mismatch/replay')
    tool=m.pkg.read(OUT/'project-tool-result.json');source=json.loads(tool['content'])['source']
    call=m.pkg.read(OUT/'project-tool-started.json')
    expected='\n'.join(f'{i+1}: {line}' for i,line in enumerate((ROOT/'docs/MODEL.md').read_text().splitlines()[:24]))
    m.g.require(call['call_id']==tool['tool_call_id'] and source['sha256']==m.pkg.sha(ROOT/'docs/MODEL.md') and
        source['text']==expected,'tool did not read the actual project document')
    answer=m.pkg.read(OUT/'project-answer-response.json')['message']['content']
    s.a.validate_answer(answer,{source['citation']},1)
    m.g.require('Qwen4ExpForConditionalGeneration' in answer,'project answer quality')
    final=m.pkg.read(OUT/'final-metrics-00.json')['metrics']
    m.g.require(all(final[n]==0 for n in s.a.EMPTY) and final['q4t_seq_slots_free']==1 and
        final['q4t_requests_total']==8 and final['q4t_requests_success_total']==6 and
        final['q4t_requests_aborted_total']==2 and final['q4t_requests_error_total']==0 and
        final['q4t_structured_workers_started_total']==final['q4t_structured_workers_reaped_total']==3,
        'final accounting/owners differ')
    backends=[]
    for index in range(1,5):
        ready=m.pkg.read(OUT/f'front-{index}-ready.json')
        backends.append(ready['control']['binding']['backend'])
        m.g.require(ready['claim_fds'] and m.pkg.read(OUT/f'front-{index}-gone.json')['passed'],
                    'claim inheritance or group/listener cleanup missing')
        state=m.status(OUT/f'front-{index}')
        if index==2:
            m.g.require(state['state']=='OWNER_ABSENT' and not state['cleanup_confirmed'] and
                not (OUT/'front-2/terminal.json').exists(),'missing owner was rewritten as clean')
        else:
            m.g.require(state['cleanup_confirmed'] and state['starts']==1 and state['restarts']==0 and
                not state['backend_stopped_by_gateway'] and
                state['state']==('STOPPED' if index==1 else 'FAILED'),'wrong gateway terminal')
    m.g.require(all(v==backends[0] for v in backends) and
        not list(OUT.glob('*auxiliary-cleanup.json')),'backend changed or cleanup assisted')
    terminal=m.pkg.read(OUT/'managed-run/terminal.json')
    m.g.require(terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and
        terminal['worker']['attempts']==1 and terminal['worker']['restarts']==0,'model lifecycle changed')
    guard=guard_archive(OUT/'managed-run/lease/events.jsonl',terminal['guard'])
    custody=m.pkg.read(OUT/'custody/exit.json');binding=m.pkg.read(OUT/'custody/lease-copy.json')
    rows=[json.loads(line) for line in (OUT/'custody/events.jsonl').read_text().splitlines()]
    m.g.require(custody['status']=='COMPLETED' and custody['cleanup_confirmed'] and
        custody['samples']==len(rows) and binding['inode']==custody['lock_inode']==3294067 and
        all('observation' in row and row['observation']['conflicts']==[] for row in rows),
        'independent custody incomplete/conflicted')
    m.g.require(rows[0]['monotonic']<terminal['guard']['finished']['monotonic']<rows[-1]['monotonic'] and
        all(0<=b['monotonic']-a['monotonic']<=10 for a,b in zip(rows,rows[1:])),
        'custody did not span production guard exit')
    gpu=m.pkg.read(OUT/'gpu-final.json')
    m.g.require(gpu['status']=='OK' and not gpu['pids'],'GPU cleanup missing')
    return dict(passed=True,successful_requests=6,interrupted_requests=2,real_tools=1,
        model_starts=1,gateway_starts=4,grammar_started_reaped=3,backend_identity=backends[0],
        independent_owner_absent_preserved=True,guard=guard,custody_samples=len(rows),
        gpu_window_seconds=result['gpu_window_elapsed'],elapsed_seconds=result['elapsed_seconds'],
        explicit_controls=result['counts']['controls'],new_model_or_http=0,
        performance_acceptance=False,actual_lan_accepted=False,long_test_started=False)


def secrets():
    values=[]
    for folder in (BASE,ROOT/'build/managed-gateway-package-tests'):
        for path in folder.rglob('policy*.json'):
            values.extend(c['api_key'].encode() for c in m.pkg.read(path).get('clients',[]))
    return values


def postflight():
    plan=m.pkg.read(s.PLAN)
    for index in range(1,5):
        ready=m.pkg.read(OUT/f'front-{index}-ready.json')
        m.g.require(not m.alive(ready['control']['owner']) and
            not m.sup.processes.group_members(ready['status']['observation']['nginx']['pid']),
            'owned frontend still alive')
    for owner in m.pkg.read(OUT/'model-owners.json'):m.g.require(not m.alive(owner),'owned model still alive')
    lock=s.a.guard.common_lock()
    m.g.require(lock.stat().st_ino==3294067 and s.a.lock_available(lock) and
        not os.path.lexists(lock.with_name(lock.name+'.recovery-required')),'original lease unavailable')
    unit=subprocess.check_output(['systemctl','--user','show',plan['unit'],'-p','LoadState','-p','MainPID'],text=True)
    m.g.require('LoadState=not-found' in unit and 'MainPID=0' in unit,'temporary unit remains')
    soar='soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'
    mask=Path.home()/'.config/systemd/user'/soar
    m.g.require(mask.is_symlink() and os.readlink(mask)=='/dev/null','SOAR mask changed')
    state=subprocess.check_output(['systemctl','--user','show',soar,'-p','MainPID','-p','ActiveState',
                                   '-p','UnitFileState'],text=True)
    m.g.require(all(x in state for x in ('MainPID=0','ActiveState=inactive','UnitFileState=masked')),'SOAR running')
    containers=subprocess.check_output(['sudo','-n','docker','ps','--format','{{.ID}} {{.Names}}'],text=True)
    m.g.require(not containers.strip(),'container state changed')
    s.a.require_idle(BASE/'postflight-gpu.json')
    m.g.require(not (ROOT/'.q4t-work/serve-soak-20261103/day/execution').exists(),'day unexpectedly started')
    result=subprocess.run([sys.executable,'-B','tools/serve_soak/stage.py','check','--profile','day'],
        capture_output=True,text=True,timeout=20)
    m.g.require(result.returncode==0,'day source identity changed')
    h.save(BASE/'day-check.json',dict(returncode=result.returncode,stdout=result.stdout,stderr=result.stderr))
    return dict(passed=True,unit=unit,soar=state,containers=containers,lock_available=True,
        lock_inode=lock.stat().st_ino,all_owned_processes_gone=True,day_started=False,
        default_binary_sha256=m.pkg.sha(ROOT/'build/q4t'),
        default_sidecar_sha256=m.pkg.sha(ROOT/'build/q4t.release.json'))


def verify_archive():
    h.verify_seal(DEST);manifest=m.pkg.read(DEST/'manifest.json')
    expected={r['member']:r for r in manifest['records']};seen=set();keys=secrets()
    with tarfile.open(DEST/'raw-records.tar.gz','r:gz') as archive:
        for entry in archive:
            m.g.require(entry.isfile() and entry.name in expected and entry.name not in seen,'unexpected archive member')
            raw=archive.extractfile(entry).read();row=expected[entry.name]
            m.g.require(len(raw)==row['bytes'] and hashlib.sha256(raw).hexdigest()==row['sha256'] and
                not any(key in raw for key in keys),'archive changed or contains key')
            h.check_identity(row);seen.add(entry.name)
    m.g.require(seen==expected.keys(),'archive missing records')
    for row in manifest['local_artifacts']:h.check_identity(row)
    h.check_identity(manifest['archiver'])
    for name,row in manifest['aliases'].items():
        h.check_identity(row);m.g.require(m.pkg.sha(DEST/name)==row['sha256'],'alias differs')
    return dict(check(),records=len(seen),local_artifacts=len(manifest['local_artifacts']),credentials_not_archived=True)


def archive():
    result=check();m.g.require(not DEST.exists(),'preserve prior archive')
    h.save(BASE/'verified.json',result);h.save(BASE/'postflight.json',postflight())
    DEST.mkdir();records=[];local=[];keys=secrets()
    paths=[(str(p.relative_to(BASE)),p) for p in sorted(BASE.rglob('*')) if p.is_file()]
    for folder in (ROOT/'build/managed-gateway-tests',ROOT/'build/managed-gateway-package-tests'):
        paths.extend(('cpu-fixtures/'+folder.name+'/'+str(p.relative_to(folder)),p)
                     for p in sorted(folder.rglob('*')) if p.is_file())
    with (DEST/'raw-records.tar.gz').open('xb') as output:
        with gzip.GzipFile(fileobj=output,mode='wb',mtime=0) as zipped:
            with tarfile.open(fileobj=zipped,mode='w|') as archive:
                for name,path in paths:
                    if path.is_symlink():
                        # Deliberate CPU symlink-rejection fixture, no content copy.
                        continue
                    if private_file(path) or path.name=='nginx':local.append(h.identity(path));continue
                    raw=path.read_bytes()
                    m.g.require(not any(key in raw for key in keys) and b'-----BEGIN PRIVATE KEY' not in raw,
                                'secret in proposed archive: '+name)
                    entry=tarfile.TarInfo(name);entry.size=len(raw);entry.mode=0o600;entry.mtime=0
                    archive.addfile(entry,io.BytesIO(raw));records.append(dict(h.identity(path),member=name))
    plan=m.pkg.read(s.PLAN);backend=Path(plan['package']['package'])
    local.extend(h.identity(backend/n) for n in ('q4t','manifest.json'))
    aliases={name+'.json':h.identity(path) for name,path in dict(plan=s.PLAN,result=OUT/'result.json',
        verified=BASE/'verified.json',postflight=BASE/'postflight.json').items()}
    for name,row in aliases.items():h.copy_checked(row,DEST/name)
    h.save(DEST/'manifest.json',dict(records=records,local_artifacts=local,aliases=aliases,
        archiver=h.identity(__file__),scope='Bounded loopback gateway supervision, not actual LAN/long-soak acceptance.',
        private_material='Keys, policies, private nginx configuration and syntax diagnostics remain local by hash.',
        cpu_fixture_limit='Deliberate symlink-rejection fixture is not followed or archived. Target contents remain recorded.',
        prior_cpu_failures_retained=True,long_validation_owner='user',day_started=False))
    h.seal(DEST);return verify_archive()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('check','archive','verify-archive'));args=parser.parse_args()
    print(json.dumps(check() if args.action=='check' else archive() if args.action=='archive' else verify_archive()))
