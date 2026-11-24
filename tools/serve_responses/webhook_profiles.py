"""Qualify a signed notification across the issued primary and light capacities."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

from tools.serve_live import profile_acceptance
from tools.serve_recovery import bundle, profile_live_reuse as reuse, profile_record
from tools.serve_responses.assistant_profiles import COMMON
from tools.serve_responses.beta_profile_acceptance import protocol
from tools.serve_service import package as pkg

ROOT = profile_acceptance.ROOT
RELEASE = ROOT/'docs/evidence/serve-agent-automations-20261124/delivery.json'
PROOF = ROOT/'docs/evidence/serve-webhooks-20261124/lifecycle.json'
ACTOR = ROOT/'tools/serve_responses/webhook_lifecycle_sdk.mjs'
RECEIPT = 'webhook-handoff.json'


def prepare(out, build, deployment):
    released = pkg.read(RELEASE)['publication']
    source = deployment/'build/service'
    manifest = bundle.verify(source, released['primary'])
    engine = pkg.verify(source/'engine', manifest['engine_manifest_sha256'])
    compiled = pkg.read(build/'generated/q4t/server/chat_protocol_sources.json')
    pkg.require(all(pkg.sha(ROOT/n) == digest for n,digest in compiled.items()) and
                pkg.read(PROOF)['status'] == 'ACCEPT_RESPONSE_BATCH_NOTIFICATION_AND_CONTROLLED_RESTART',
                'predecessor or compiled identity differs')
    protected = {str(deployment/'build'/n):pkg.sha(deployment/'build'/n)
                 for n in ('q4t','q4t.release.json','service/manifest.json',
                           'service.release.json','service-native-state/ledger.json')}
    profile_acceptance.prepare_profiles(out, manifest, engine,
        dict(engine['live_workers'], engine_sha256=pkg.sha(build/'q4t')), pkg.sha(build/'q4t-ingress'),
        build=build, protected=protected, installed_workers=source/'engine',
        selected_manifest=source/'manifest.json',
        previous_fixture=deployment/'.q4t-work/serve-live-20261121/fixture-business-01')
    plans = {name:pkg.read(out/name/'plan.json') for name in profile_acceptance.PROFILES}
    ports = {p[k] for p in plans.values() for k in ('port','operations_port','public_port')}
    with contextlib.ExitStack() as sockets:
        while True:
            sock = sockets.enter_context(socket.socket())
            sock.bind(('127.0.0.1',0))
            port = sock.getsockname()[1]
            if port not in ports:
                break
    ca = out/'subscriber-ca.pem'
    certs = [Path(p['tls']['certificate']).read_bytes() for p in plans.values()]
    ca.write_bytes(b'\n'.join(dict.fromkeys(certs)))
    for name,plan in plans.items():
        phase = out/name
        previous = phase/'previous-profile.json'
        shutil.copyfile(source/('profile-'+name+'.json'),previous)
        expected = pkg.sha(previous)
        pkg.require(expected == reuse.AUTOMATION_REVIEWED[name] ==
                    manifest['recovery_profiles']['qualifications'][name]['evidence_sha256'],
                    'capacity does not match the issued automation release')
        ingress = phase/'engine/q4t-ingress'
        shutil.copy2(plan['ingress'],ingress)
        clients = pkg.read(phase/'client-policy.json')
        for client in clients['clients']:
            if client['id'] in ('alpha','beta'):
                client.update(max_output_tokens=512, requests_per_minute=250,
                              tokens_per_minute=32000, max_prompt_tokens=8192)
        pkg.save(phase/'client-policy.json',clients)
        shutil.copy2(phase/'client-policy.json',phase/'engine/client-policy.json')
        path = phase/'engine/webhook-policy.json'
        pkg.save(path,dict(version=1,network=dict(public_networks=False,allowed_hosts=['127.0.0.1'],
            allowed_networks=['127.0.0.1/32'],ports=[port],ca_file=str(ca),timeout_ms=3500)))
        path.chmod(0o400)
        binding = pkg.webhook_policy_binding(plan['binary'],path,path.read_bytes())
        settings = dict(engine['settings'],profile=manifest['recovery_profiles']['profiles'][name],
                        port=plan['port'],operations_port=plan['operations_port'])
        settings['application_state'] = dict(settings['application_state'],directory='',live_recordings=True)
        core = dict(engine,settings=settings,live_workers=plan['live_workers'],webhook_policy=binding)
        command = pkg.command(path.parent,core,state_directory=out/'state')
        i = command.index('--webhook-policy-file')
        pkg.require(command[i+1] == str(path) and command[:i]+command[i+2:] == plan['command'],
                    'policy changed other effective model arguments')
        plan.update(ingress=str(ingress),command=command,
            webhook_handoff=dict(port=port,ca=str(ca),ca_sha256=pkg.sha(ca),policy=binding,
                previous=expected,release=released['primary'],protocol=protocol(compiled)),
            client_policy_sha256=pkg.sha(phase/'client-policy.json'),
            installed_policy_sha256=pkg.sha(phase/'engine/client-policy.json'))
        for file in (ACTOR,Path(__file__),PROOF,RELEASE,path,ca):
            plan['sources'][str(file.relative_to(ROOT))] = pkg.sha(file)
        pkg.save(phase/'plan.json',plan)


class Subscriber:
    """Keep the signing key in this driver's memory between capacity fixtures."""
    def __init__(self):
        self.handoff = None

    def business(self, stage, capabilities):
        plan = stage.plan
        primary = plan['name'] == 'primary'
        change = plan['webhook_handoff']
        first = pkg.read(stage.out.parent/'primary/plan.json')
        params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1",
            key=stage.keys['alpha'],other=stage.keys['beta'],sdk=plan['sdk_directory'],
            subscriberPort=change['port'],tlsCert=first['tls']['certificate'],tlsKey=first['tls']['key'],
            model='qwen3.8-flash-next',revision=pkg.read(RELEASE)['publication']['publication_revision'],
            phase='prepare' if primary else 'restore')
        if not primary:
            pkg.require(self.handoff is not None,'missing subscriber credentials')
            params['previous'] = self.handoff
        with (stage.out/'subscriber.stderr').open('xb') as log:
            child = subprocess.run(['node',ACTOR],input=json.dumps(params).encode(),
                stdout=subprocess.PIPE,stderr=log,timeout=150,
                env=dict(os.environ,NODE_EXTRA_CA_CERTS=change['ca']))
        pkg.require(child.returncode==0 and len(child.stdout)<=16384,'subscriber SDK failed')
        row = json.loads(child.stdout)
        if primary:
            self.handoff = row['handoff']
            row = row['receipt']
        else:
            self.handoff = None
        if primary:
            root = Path(plan['command'][plan['command'].index('--object-store-dir')+1])
            until = time.monotonic()+10
            while time.monotonic()<until:
                found = list(root.glob('*.webhook_event.'+row['event']+'.json'))
                if len(found)==1:
                    data = pkg.read(found[0])['record']['data']
                    first = data['deliveries'][0]
                    if first.get('http_status') == 503 and first.get('status') == 'pending':
                        row['persisted'] = dict(event=data['event'],attempts=first['attempts'])
                        break
                time.sleep(.1)
        until = time.monotonic()+8
        while time.monotonic()<until:
            status,raw = stage.http('/v1/config')
            pkg.require(status==200,'configuration unavailable after delivery')
            config = json.loads(raw)
            counters = config.get('webhook_delivery',{})
            if counters.get('retries' if primary else 'acknowledged',0) >= (1 if primary else 2):
                row['startup'] = config['application_startup']['webhook_dispatcher']
                row['counters'] = counters
                break
            time.sleep(.15)
        pkg.save(stage.out/RECEIPT,row)
        verify(stage.out,plan,capabilities)


def verify(phase, plan, capabilities):
    name = plan['name']
    change = plan['webhook_handoff']
    prior = profile_record.read(phase/'previous-profile.json')
    release = pkg.read(RELEASE)['publication']
    expected = dict(prior['capabilities'],webhooks=reuse.WEBHOOK_CAPABILITY,
                    protocol_source_sha256=change['protocol'])
    pkg.require(pkg.sha(phase/'previous-profile.json') == change['previous'] == reuse.AUTOMATION_REVIEWED[name]
                and change['release']==plan['selected_sha256']==release['primary']
                and pkg.same_json(expected,capabilities) and plan['capacity']==prior['capacity']
                and pkg.sha(change['ca'])==change['ca_sha256']
                and pkg.sha(phase/'engine/webhook-policy.json')==change['policy']['sha256'],
                'webhook declaration, policy, capacity or prior release differs')
    row = pkg.read(phase/RECEIPT)
    pkg.require(row.get('startup',{}).get('state')=='ready'
                and row['startup']['readiness_dependency'] is False
                and row['counters']['capacity_losses']==0
                and row['counters']['retries' if name=='primary' else 'acknowledged'] >=
                    (1 if name=='primary' else 2), 'webhook worker or delivery counters differ')
    rev = str(release['publication_revision'])
    pkg.require(row['sdk']=='7.47.0' and row['responseAnswer']==rev,'SDK or response result differs')
    if name=='primary':
        pkg.require(row['status']==503 and row['persisted']==dict(event=row['first'],attempts=1)
                    and row['first']['id']==row['event'] and row['first']['data']['id']==row['response'],
                    'primary did not persist the retry after subscriber refusal')
    else:
        first = pkg.read(phase.parent/'primary'/RECEIPT)
        a,b,c = row['deliveries']
        pkg.require(row['peer']==404 and row['batchAnswer']==rev and row['independent_chat']=='42'
            and first['endpoint']==row['endpoint'] and first['response']==row['response']
            and a['event']==b['event']==first['first'] and a['message']==b['message']==first['event']
            and [x['status'] for x in (a,b,c)]==[503,202,202]
            and c['event']['data']['id']==row['batch'] and c['event']['type']=='batch.completed',
            'light failed to complete the pending delivery or new Batch')
    inherited = prior['business']['live']['evidence_sha256']
    pkg.require(COMMON <= inherited.keys() and 'automation-handoff.json' in inherited,'missing issued evidence')
    return {**{k:v for k,v in inherited.items() if k not in COMMON},RECEIPT:pkg.sha(phase/RECEIPT)}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    for key in ('output','build','deployment'):
        ap.add_argument('--'+key,type=Path,required=True)
    args = ap.parse_args()
    out = args.output.resolve()
    pkg.require(out.is_relative_to(ROOT/'.q4t-work'),'private evidence directory required')
    prepare(out,args.build.resolve(strict=True),args.deployment.resolve(strict=True))
