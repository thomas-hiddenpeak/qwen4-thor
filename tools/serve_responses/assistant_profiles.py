"""Qualify a persisted Assistants tool handoff on the primary and light release profiles."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from tools.serve_live import profile_acceptance
from tools.serve_recovery import bundle, profile_live_reuse, profile_record
from tools.serve_responses.beta_profile_acceptance import protocol
from tools.serve_responses.vault_profile_acceptance import prepare_peer
from tools.serve_service import package as pkg

ROOT = profile_acceptance.ROOT
SDK = ROOT/'tools/serve_responses/assistant_handoff_sdk.mjs'
PROOF = ROOT/'docs/evidence/serve-assistants-20261124/runs.json'
RELEASE = ROOT/'docs/evidence/serve-audio-translation-20261124/delivery.json'
COMMON = {'configuration.wire', 'capabilities.json', 'core-after.wire',
          'observation.json', 'result.json', 'cleanup.json', 'plan.json'}


def prepare(out, build):
    source = ROOT/'build/service'
    digest = pkg.read(RELEASE)['publication']['primary']
    manifest = bundle.verify(source, digest)
    engine = pkg.verify(source/'engine', manifest['engine_manifest_sha256'])
    proof = pkg.read(PROOF)
    compiled = build/'generated/q4t/server/chat_protocol_sources.json'
    sources = pkg.read(compiled)
    pkg.require(proof['lease'] == 'COMPLETED' and proof['run']['status'] == 'PASS' and
                proof['engine'] == pkg.sha(build/'q4t') and proof['ingress'] == pkg.sha(build/'q4t-ingress')
                and proof['compiled_sources'] == pkg.sha(compiled) and proof['protocol'] == protocol(sources)
                and all(pkg.sha(ROOT/n) == h for n,h in sources.items()), 'Runs candidate changed')
    protected = {str(ROOT/'build'/n):pkg.sha(ROOT/'build'/n) for n in (
        'q4t','q4t.release.json','service/manifest.json','service.release.json','service-native-state/ledger.json')}
    profile_acceptance.prepare_profiles(out,manifest,engine,dict(engine['live_workers'],engine_sha256=proof['engine']),
                                        proof['ingress'],build=build,protected=protected,installed_workers=source/'engine')
    prepare_peer(out,renewal=True)
    for name in profile_acceptance.PROFILES:
        phase = out/name
        plan = pkg.read(phase/'plan.json')
        previous = phase/'previous-profile.json'
        shutil.copyfile(source/('profile-'+name+'.json'),previous)
        prior = pkg.sha(previous)
        pkg.require(prior == profile_live_reuse.TRANSLATION_REVIEWED[name] ==
                    manifest['recovery_profiles']['qualifications'][name]['evidence_sha256'],
                    'selected release predecessor changed')
        ingress = phase/'engine/q4t-ingress'
        shutil.copy2(plan['ingress'],ingress)
        policy = pkg.read(phase/'client-policy.json')
        owner = next(c for c in policy['clients'] if c['id']=='alpha')
        owner.update(requests_per_minute=60,tokens_per_minute=32000,max_prompt_tokens=8192)
        pkg.save(phase/'client-policy.json',policy)
        shutil.copy2(phase/'client-policy.json',phase/'engine/client-policy.json')
        plan.update(ingress=str(ingress),assistant_handoff=dict(previous=prior,protocol=proof['protocol'],
                    release=digest),client_policy_sha256=pkg.sha(phase/'client-policy.json'),
                    installed_policy_sha256=pkg.sha(phase/'engine/client-policy.json'))
        plan['command'].extend(['--mcp-policy-file',str(out/'mcp/policy.json')])
        for path in (SDK,Path(__file__),PROOF,RELEASE):
            plan['sources'][str(path.relative_to(ROOT))] = pkg.sha(path)
        pkg.save(phase/'plan.json',plan)


def business(stage,capabilities):
    name = stage.plan['name']
    params = dict(base=f"https://127.0.0.1:{stage.plan['public_port']}/v1",key=stage.keys['alpha'],
                  other=stage.keys['beta'],sdk=stage.plan['sdk_directory'],source=str(RELEASE),
                  source_sha256=pkg.sha(RELEASE),phase='create' if name=='primary' else 'resume')
    if name=='light':
        params['prior'] = pkg.read(stage.out.parent/'primary/assistant-handoff.json')
    result = subprocess.run(['node',SDK],input=json.dumps(params),text=True,capture_output=True,timeout=100,
        env=dict(os.environ,NODE_EXTRA_CA_CERTS=stage.plan['tls']['certificate']))
    (stage.out/'assistant-handoff.stderr').write_text(result.stderr)
    pkg.require(result.returncode==0,'SDK handoff failed on '+name+'; see assistant-handoff.stderr')
    pkg.save(stage.out/'assistant-handoff.json',json.loads(result.stdout))
    verify(stage.out,stage.plan,capabilities)


def verify(phase,plan,capabilities):
    name=plan['name']
    change=plan['assistant_handoff']
    old=phase/'previous-profile.json'
    prior=profile_record.read(old)
    declared=dict(capabilities)
    proof=pkg.read(PROOF)
    pkg.require(pkg.sha(old)==change['previous']==profile_live_reuse.TRANSLATION_REVIEWED[name] and
                change['release']==pkg.read(RELEASE)['publication']['primary'] and
                pkg.same_json(declared.pop('assistants',None),profile_live_reuse.ASSISTANTS_CAPABILITY) and
                declared.pop('protocol_source_sha256')==change['protocol']==proof['protocol'] and
                pkg.same_json(declared,{k:v for k,v in prior['capabilities'].items() if k!='protocol_source_sha256'}) and
                plan['capacity']==prior['capacity'] and plan['binary_sha256']==proof['engine'] and
                plan['ingress_sha256']==proof['ingress'],'Run profile identity or retained capability drift')
    row=pkg.read(phase/'assistant-handoff.json')
    pkg.require(row['sdk']==proof['sdk']['sdk'] and row['source_sha256']==pkg.sha(RELEASE) and
                row['revision']==pkg.read(RELEASE)['publication']['publication_revision'] and
                row['foreign_status']==404 and json.loads(row['arguments'])=={} and
                all(row[k].startswith(prefix) for k,prefix in [('run','run_'),('thread','thread_'),
                    ('assistant','asst_'),('step','step_')]) and row['call'],'invalid owned Run handoff')
    if name=='primary':
        pkg.require(row['status']=='requires_action' and row['events'][-1]=='thread.run.requires_action' and
                    'thread.run.step.delta' in row['events'],'primary did not persist an actual tool call')
    else:
        previous=pkg.read(phase.parent/'primary/assistant-handoff.json')
        pkg.require(all(row[k]==previous[k] for k in ('assistant','run','thread','step','call','arguments')) and
                    row['status']=='completed' and row['events'][-1]=='thread.run.completed' and
                    row['events'].count('thread.run.step.completed')==2 and row['total_tokens']>0 and
                    row['answer']==str(row['revision']),'light did not finish the pending primary call')
    evidence=prior['business']['live']['evidence_sha256']
    pkg.require(COMMON <= evidence.keys(),'missing prior profile observations')
    return dict({k:v for k,v in evidence.items() if k not in COMMON},
                **{'assistant-handoff.json':pkg.sha(phase/'assistant-handoff.json')})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    out=args.output.resolve()
    pkg.require(out.is_relative_to(ROOT/'.q4t-work'),'fresh private output required')
    prepare(out,args.build.resolve(strict=True))
