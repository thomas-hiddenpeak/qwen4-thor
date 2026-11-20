"""Owned official WSS duplex work on both exact managed capacities."""
import hashlib
import json
import os
from pathlib import Path
import re
import time
from tools.serve_realtime import duplex_acceptance, duplex_checks
from tools.serve_realtime.ingress_fixture import NativeIngress, certificate
from tools.serve_recovery import profile_realtime, profile_duplex_evidence as evidence
from tools.serve_service.package import read, require, save, sha

ROOT = Path(__file__).resolve().parents[2]
POSTS_PER_PROFILE = 16  # Eleven manual and five VAD native ASR/response admissions.


def prepare(source, output, public_port, sdk):
    source=Path(source).resolve(strict=True);plan=read(source)
    require(plan['schema']=='q4t-realtime-idle-followup-v1' and
        read(source.parent/'result.json')['status']=='PASS' and
        read(source.parent.parent/'lease/exit.json')['status']=='COMPLETED',
        'qualified actual native duplex follow-up required')
    directory=profile_realtime.reuse_sdk(plan,sdk)
    tls=certificate(Path(output)/'duplex-credentials')
    record=ROOT/'docs/STATUS.md';match=re.search(r'默认\s*rev([0-9]+)',record.read_text())
    require(match is not None,'actual project release missing')
    clip=plan['clips']['zh']
    files={str(p):sha(p) for p in (source,source.parent/'result.json',
        source.parent.parent/'lease/exit.json',Path(plan['ingress']),record)}
    files.update({clip['path']:clip['sha256'],clip['source']:clip['source_sha256']})
    files.update({tls[n]:sha(tls[n]) for n in ('certificate','key')})
    return dict(sdk_directory=directory,ingress=plan['ingress'],public_port=public_port,
        tls=tls,clip=clip,files=files,source=dict(path='docs/STATUS.md',sha256=sha(record),
                                              version='rev'+match[1]))


def save_observed(path,value):
    raw=evidence.ledger.canonical(value)+b'\n'
    require(len(raw)<=4<<20, 'duplex original receipt size limit')
    with Path(path).open('xb') as output:
        output.write(raw);output.flush();os.fsync(output.fileno())


def read_observed(path):
    path=Path(path)
    require(path.stat().st_size<=4<<20, 'duplex original receipt size limit')
    return json.loads(path.read_text())


def run(stage):
    sessions=run_sessions(stage, ('manual','vad'))
    value=dict(format=evidence.FORMAT,source=stage.plan['duplex']['source'],sessions=sessions)
    save_observed(stage.out/(stage.profile_name+'-duplex-observed.json'),value)
    evidence.business(value);return value


def run_sessions(stage, modes):
    require(modes in (('manual','vad'),('mixed','vad_once'),
                     ('speech_tool','barge_in'),('barge_in',)),
            'fixed duplex session scope')
    plan=stage.plan['duplex'];label=stage.profile_name+'-duplex'
    for path,digest in plan['files'].items():require(sha(path)==digest,'duplex fixture drift')
    policy=read(stage.out/'client-policy.json')
    clients={profile_realtime.encrypted.CLIENT:hashlib.sha256(os.urandom(32)).hexdigest(),
             'audio':hashlib.sha256(os.urandom(32)).hexdigest()}
    stage.budget('ingress_starts')
    edge=NativeIngress(stage.out/label,plan['ingress'],plan['public_port'],
        stage.plan['port'],stage.plan['operations_port'],policy['gateway_token'],clients,
        plan['tls'],lambda:stage.model.poll() is None)
    guard=profile_realtime.encrypted.foreground.base.lifecycle.guard
    stage.history[edge.process.pid]=guard.processes()[edge.process.pid]
    sessions={}
    try:
        for mode in modes:
            case=label+'-'+mode
            client=profile_realtime.encrypted.CLIENT if mode in ('manual','mixed','speech_tool') else 'audio'
            def observe(suffix):
                status,raw=stage.http('/metrics',label=case+'-'+suffix)
                require(status==200,'duplex actual metrics unavailable')
                return profile_realtime.state(
                    profile_realtime.encrypted.foreground.base.lifecycle.parse_metrics(raw),client)
            before=observe('before')
            stage.budget('websocket_sessions')
            for _ in range(dict(manual=11,vad=5,mixed=3,vad_once=2,
                                speech_tool=6,barge_in=2)[mode]):stage.budget('chats')
            clip=plan['clip'];record=plan['source']
            args=dict(mode=mode,base_url=f"https://127.0.0.1:{plan['public_port']}/v1",
                api_key=clients[client],ca_file=plan['tls']['certificate'],clip=clip['path'],
                clip_sha256=clip['sha256'],expected=evidence.PHRASE,
                record_file=str(ROOT/record['path']),record_sha256=record['sha256'],
                expected_version=record['version'],journal_file=str(stage.out/(case+'.events.jsonl')))
            if 'instruction_policy' in plan:
                args['instruction_policy']=plan['instruction_policy']
            env={k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD')}
            env['Q4T_NODE_SDK_DIR']=plan['sdk_directory']
            out=stage.out/(case+'.sdk.json')
            process=duplex_acceptance.capture_actor([stage.plan['sdk']['node']['path'],
                str(ROOT/'tools/serve_realtime/duplex_sdk.mjs')],args,out,
                stage.out/(case+'.stderr'),env,430)
            require(process.returncode==0 and out.stat().st_size<=4<<20,'actual duplex SDK failure')
            session=json.loads(out.read_text())
            journal=Path(args['journal_file']);require(journal.stat().st_size<=32<<20,'journal bound')
            duplex_checks.journal_matches([json.loads(l) for l in journal.read_text().splitlines()],session)
            stage.same_model();stage.drained(case+'-drain')
            end=time.monotonic()+5;index=0;after=observe('after-0')
            while (after['sessions'] or after['host_bytes']) and time.monotonic()<end:
                time.sleep(.02);index+=1;after=observe('after-'+str(index))
            sessions[mode]=dict(session=session,before=before,after=after)
        save_observed(stage.out/(label+'-sessions-observed.json'),sessions)
        return sessions
    finally:
        edge.close()
        save(stage.out/(label+'-entry-cleanup.json'),dict(exit_code=edge.process.returncode,
             pid=edge.process.pid,port_released=True))
