"""Qualify concurrent file completion and receipt continuity across capacity profiles."""
import base64
import json
from pathlib import Path
import re
import shutil
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

ROOT = lifecycle.ROOT
ACTOR = ROOT/'tools/serve_audit/concurrent_upload_business.py'
PROOF = ROOT/'docs/evidence/serve-upload-completion-race-20261128/device.json'
DOCUMENT = ROOT/'docs/evidence/serve-upload-sdk-recovery-20261128/project-release.json'
KEY = 'concurrent_upload_completion'
RECEIPT = 'upload-completion-handoff.json'


def prepare(out, build, deployment, sdk_python):
    proof = package.read(PROOF)
    package.require(proof['engine'] == package.sha(build/'q4t') and
                    proof['protocol_sources'] == package.sha(build/'generated/q4t/server/chat_protocol_sources.json'),
                    'candidate build differs from device acceptance')
    lifecycle.prepare_from_manifest(out, build, deployment, proof['predecessor_manifest'],
                                    reuse.PENDING_BROWSER_REVIEWED, KEY, [PROOF, DOCUMENT, ACTOR, Path(__file__)])
    issued_clients = package.read(deployment/'build/service/engine/client-policy.json')['clients']
    owner = next(client for client in issued_clients if client['id'] == 'alpha')
    package.require(owner['max_inflight'] >= 2, 'issued owner cannot admit two simultaneous uploads')
    for name in ('primary','light'):
        phase = out/name
        plan = package.read(phase/'plan.json')
        policy = package.read(phase/'client-policy.json')
        next(c for c in policy['clients'] if c['id']=='alpha')['max_inflight'] = 2
        package.save(phase/'client-policy.json', policy)
        shutil.copy2(phase/'client-policy.json', phase/'engine/client-policy.json')
        plan.update(client_policy_sha256=package.sha(phase/'client-policy.json'),
                    installed_policy_sha256=package.sha(phase/'engine/client-policy.json'))
        plan[KEY].update(python=str(Path(sdk_python).absolute()),
                         sdk_requests=16 if name == 'primary' else 20)
        plan['scope'] = ('Concurrent upload completion via official SDK, durable file use, '
                         'capacity profile controls and an independent Chat.')
        package.save(phase/'plan.json',plan)


def check(row, name, previous=None):
    count = {'single':17, 'primary':16, 'light':20}[name]
    requests = row['requests']
    package.require(row['phase'] == name and row['version'] == '3.55.1' and
                    len(requests) == count and len({r['trace'] for r in requests if r['trace']}) == count and
                    row['source_sha256'] == package.sha(DOCUMENT) and row['response'] == str(
                        package.read(DOCUMENT)['version_revision']) and row['independent_chat'] == '42',
                    'SDK, original release or HTTP accounting differs')
    pairs = row['pairs']
    package.require(len(pairs)==2 and pairs[0]['same_parameters'] is True and
                    pairs[1]['same_parameters'] is False and pairs[0]['file'] != pairs[1]['file'],
                    'both concurrent request pairs required')
    for pair in pairs:
        require = [200,200] if pair['same_parameters'] else [200,409]
        target = '/v1/uploads/'+pair['upload']+'/complete'
        calls = [r for r in requests if r['path']==target and r['method']=='POST']
        package.require(re.fullmatch('file_[a-zA-Z0-9_-]+',pair['file']) and
                        sorted(r['status'] for r in calls)==require and
                        max(r['start_ns'] for r in calls)<min(r['headers_ns'] for r in calls) and
                        sorted(r['status'] for r in pair['replies'])==require and
                        all(r.get('file')==pair['file'] if r['status']==200
                            else r.get('code')=='upload_already_completed' for r in pair['replies']),
                        'concurrent requests or published identity differ')
    if name in ('single', 'primary'):
        package.require(previous is None and row['restored'] is None, 'unexpected prior upload')
    else:
        check(previous,'primary')
        first = previous['pairs'][0]
        package.require(row['restored'] == first['file'] and [
            (r['method'],r['path'],r['status']) for r in requests[:3]] == [
                ('POST', '/v1/uploads/'+first['upload']+'/complete',200),
                ('GET','/v1/files/'+first['file']+'/content',200),
                ('DELETE','/v1/files/'+first['file'],200)], 'light did not recover the primary upload')


def business(stage):
    name = stage.plan['name']
    prior = package.read(stage.out.parent/'primary'/RECEIPT) if name == 'light' else None
    cfg = dict(base=f"https://127.0.0.1:{stage.plan['public_port']}/v1", key=stage.keys['alpha'],
               peer=stage.keys['beta'], certificate=stage.plan['tls']['certificate'], phase=name,
               previous=prior, model='qwen3.8-flash-next', source=base64.b64encode(DOCUMENT.read_bytes()).decode())
    with (stage.out/'upload-sdk.stderr').open('xb') as errors:
        job=subprocess.run([stage.plan[KEY]['python'],'-I','-B',ACTOR],input=json.dumps(cfg).encode(),
                           stdout=subprocess.PIPE,stderr=errors,timeout=125)
    package.require(job.returncode==0,'upload completion SDK failed; see upload-sdk.stderr')
    row=json.loads(job.stdout)
    check(row,name,prior)
    package.save(stage.out/RECEIPT,row)


def verify(phase, plan, capabilities, current):
    previous_file = phase/'previous-profile.json'
    previous = profile_record.read(previous_file)
    name = plan['name']
    change = plan[KEY]
    proof = package.read(PROOF)
    package.require(package.sha(previous_file)==change['previous']==reuse.PENDING_BROWSER_REVIEWED[name]
                    and proof['predecessor_manifest']==change['release']==plan['selected_sha256']
                    and proof['engine']==plan['binary_sha256'] and plan['capacity']==previous['capacity']
                    and all(plan['sources'][str(p.relative_to(ROOT))]==package.sha(p)
                            for p in (DOCUMENT,PROOF,ACTOR,Path(__file__)))
                    and package.same_json(capabilities,dict(previous['capabilities'],
                                                          protocol_source_sha256=change['protocol'])),
                    'tested source, capacity or protocol identity differs')
    prior = package.read(phase.parent/'primary'/RECEIPT) if name=='light' else None
    check(package.read(phase/RECEIPT),name,prior)
    inherited=previous['business']['live']['evidence_sha256']
    package.require(reuse.PENDING_BROWSER_RECEIPT in inherited,'prior qualification missing')
    return {**{k:v for k,v in inherited.items() if k not in current},RECEIPT:package.sha(phase/RECEIPT)}
