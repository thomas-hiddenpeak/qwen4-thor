"""Qualify the durable automation queue across the issued model profiles."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from tools.serve_live import profile_acceptance
from tools.serve_recovery import bundle, profile_live_reuse as reuse, profile_record
from tools.serve_responses.assistant_profiles import COMMON
from tools.serve_responses.beta_profile_acceptance import protocol
from tools.serve_service import package as pkg

ROOT = profile_acceptance.ROOT
PROOF = ROOT/'docs/evidence/serve-agent-automations-20261124/session-tool.json'
RELEASE = ROOT/'docs/evidence/serve-realtime-translation-20261124/delivery.json'
ACTOR = ROOT/'tools/serve_responses/agent_automation_handoff_sdk.mjs'
RECEIPT = 'automation-handoff.json'


def prepare(out, build, deployment):
    release = pkg.read(RELEASE)['publication']
    source = deployment/'build/service'
    manifest = bundle.verify(source, release['primary'])
    engine = pkg.verify(source/'engine', manifest['engine_manifest_sha256'])
    proof = pkg.read(PROOF)
    identity = build/'generated/q4t/server/chat_protocol_sources.json'
    sources = pkg.read(identity)
    candidate = proof['candidate']
    pkg.require(proof['status'] == 'PASS_HOSTED_SESSION_WAKEUP_NATIVE_TLS' and
                proof['protocol_sources_sha256'] == pkg.sha(identity) and
                all(pkg.sha(ROOT/name) == digest for name,digest in sources.items()) and
                candidate['binary'] == pkg.sha(build/'q4t') and
                candidate['ingress'] == pkg.sha(build/'q4t-ingress'),
                'automation capacity needs the qualified session candidate')
    protected = {str(deployment/'build'/n):pkg.sha(deployment/'build'/n)
                 for n in ('q4t','q4t.release.json','service/manifest.json',
                           'service.release.json','service-native-state/ledger.json')}
    profile_acceptance.prepare_profiles(out, manifest, engine,
        dict(engine['live_workers'], engine_sha256=candidate['binary']), candidate['ingress'],
        build=build, protected=protected, installed_workers=source/'engine',
        selected_manifest=source/'manifest.json',
        previous_fixture=deployment/'.q4t-work/serve-live-20261121/fixture-business-01')
    for name in profile_acceptance.PROFILES:
        phase = out/name
        plan = pkg.read(phase/'plan.json')
        previous = phase/'previous-profile.json'
        shutil.copyfile(source/('profile-'+name+'.json'), previous)
        anchor = pkg.sha(previous)
        pkg.require(anchor == reuse.TRANSLATOR_REVIEWED[name] ==
                    manifest['recovery_profiles']['qualifications'][name]['evidence_sha256'],
                    'selected release capacity differs from the reviewed predecessor')
        ingress = phase/'engine/q4t-ingress'
        shutil.copy2(plan['ingress'], ingress)
        policy = pkg.read(phase/'client-policy.json')
        for client in policy['clients']:
            if client['id'] in ('alpha','beta'):
                client.update(max_output_tokens=512, requests_per_minute=250,
                              tokens_per_minute=32000, max_prompt_tokens=8192)
        pkg.save(phase/'client-policy.json', policy)
        shutil.copy2(phase/'client-policy.json',phase/'engine/client-policy.json')
        plan.update(ingress=str(ingress), automation_handoff=dict(previous=anchor,
                    release=release['primary'], protocol=protocol(sources)),
                    client_policy_sha256=pkg.sha(phase/'client-policy.json'),
                    installed_policy_sha256=pkg.sha(phase/'engine/client-policy.json'))
        for path in (ACTOR,Path(__file__),PROOF,RELEASE):
            plan['sources'][str(path.relative_to(ROOT))] = pkg.sha(path)
        pkg.save(phase/'plan.json',plan)


def business(stage, capabilities):
    plan = stage.plan
    primary = plan['name'] == 'primary'
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1", key=stage.keys['alpha'],
                  other=stage.keys['beta'], sdk=plan['sdk_directory'],
                  phase='prepare' if primary else 'restore',
                  revision=pkg.read(RELEASE)['publication']['publication_revision'])
    if not primary:
        params['previous'] = pkg.read(stage.out.parent/'primary'/RECEIPT)
    process = subprocess.run(['node',ACTOR],input=json.dumps(params),capture_output=True,
        text=True,timeout=160,env=dict(os.environ,NODE_EXTRA_CA_CERTS=plan['tls']['certificate']))
    (stage.out/'automation-handoff.stderr').write_text(process.stderr)
    pkg.require(process.returncode == 0, 'automation SDK handoff failed for '+plan['name'])
    pkg.save(stage.out/RECEIPT,json.loads(process.stdout))
    verify(stage.out,plan,capabilities)


def verify(phase, plan, capabilities):
    change = plan['automation_handoff']
    name = plan['name']
    prior = profile_record.read(phase/'previous-profile.json')
    expected = dict(prior['capabilities'], agent_automations=reuse.AUTOMATION_CAPABILITY,
                    agent_sessions=reuse.automation_session(), protocol_source_sha256=change['protocol'])
    proof = pkg.read(PROOF)['candidate']
    release = pkg.read(RELEASE)['publication']
    pkg.require(pkg.sha(phase/'previous-profile.json') == change['previous'] == reuse.TRANSLATOR_REVIEWED[name]
                and plan['selected_sha256'] == change['release'] == release['primary']
                and pkg.same_json(expected,capabilities) and plan['capacity'] == prior['capacity']
                and proof['binary'] == plan['binary_sha256'] and proof['ingress'] == plan['ingress_sha256'],
                'automation declaration, capacity or candidate differs')
    row = pkg.read(phase/RECEIPT)
    pkg.require(row['sdk'] == '7.47.0' and row['peer'] == 404,'unverified owner isolation')
    if name == 'primary':
        identities = dict(session='agent_session_',turn='turn_',call='call_',automation='automation_',invocation='invocation_')
        pkg.require(row['phase'] == 'prepare' and row['queued'] is True and
                    isinstance(row['scheduled_at'],int) and
                    all(isinstance(row[k],str) and row[k].startswith(prefix)
                        for k,prefix in identities.items()),
                    'primary did not retain a pending invocation')
    else:
        old = pkg.read(phase.parent/'primary'/RECEIPT)
        revision = release['publication_revision']
        pkg.require(row['phase'] == 'restore' and pkg.same_json(row['previous'],old)
                    and row['delivered'] == old['invocation'] and row['chat'] == '42'
                    and row['final_invocations'] == [old['invocation']]
                    and len(row['turns']) == len(set(row['turns'])) == 2 and row['turns'][0] == old['turn']
                    and row['answers'] == [str(revision),str(revision+1)],
                    'light failed to resume the single pending delivery')
    inherited = prior['business']['live']['evidence_sha256']
    pkg.require(COMMON <= inherited.keys() and 'translation-transport.json' in inherited,
                'missing issued profile evidence')
    return {**{k:v for k,v in inherited.items() if k not in COMMON},RECEIPT:pkg.sha(phase/RECEIPT)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('output','build','deployment'):
        parser.add_argument('--'+key,type=Path,required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    pkg.require(out.is_relative_to(ROOT/'.q4t-work'),'private evidence directory required')
    prepare(out,args.build.resolve(strict=True),args.deployment.resolve(strict=True))
