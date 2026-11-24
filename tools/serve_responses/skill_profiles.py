"""Qualify persisted skill versions, workspace files and Run budgets across capacities."""
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
PROOF = ROOT/'docs/evidence/serve-skills-20261124/business.json'
RELEASE = ROOT/'docs/evidence/serve-assistants-20261124/delivery.json'
ACTORS = {'skill':ROOT/'tools/serve_responses/skill_handoff_sdk.mjs',
          'budget':ROOT/'tools/serve_responses/assistant_handoff_sdk.mjs'}
OLD_CAPS = '08e689d280ca868050044dbea25c8d3b29d5c8b1cea022355d151f3909c65515'
ACCEPTED_PROTOCOL = 'd62ffb8ffcafb39335a38bef6a60be581ff182fc4e8bc8dca2f162cd3f94ac14'
BUDGET = 2048


def prepare(out, build, deployment):
    source = deployment/'build/service'
    published = pkg.read(RELEASE)['publication']['primary']
    manifest = bundle.verify(source, published)
    engine = pkg.verify(source/'engine', manifest['engine_manifest_sha256'])
    proof = pkg.read(PROOF)
    sources = pkg.read(build/'generated/q4t/server/chat_protocol_sources.json')
    before = dict(sources)
    before['src/server/chat_capabilities.cpp'] = OLD_CAPS
    pkg.require(proof['status']=='ACCEPT_SKILLS_HTTP_AND_CONTAINER_WORKSPACE_SNAPSHOTS' and
                len(proof['coverage']['methods'])==11 and proof['coverage']['declared_and_official_match'] and
                protocol(before)==ACCEPTED_PROTOCOL and protocol(sources)!=ACCEPTED_PROTOCOL and
                all(pkg.sha(ROOT/n)==h for n,h in sources.items()) and
                pkg.sha(build/'q4t-ingress')==proof['ingress'],
                'profile rebuild must only change public discovery from the accepted Skills engine')
    protected = {str(deployment/'build'/n):pkg.sha(deployment/'build'/n) for n in (
        'q4t','q4t.release.json','service/manifest.json','service.release.json','service-native-state/ledger.json')}
    new_engine = pkg.sha(build/'q4t')
    profile_acceptance.prepare_profiles(out,manifest,engine,dict(engine['live_workers'],engine_sha256=new_engine),
        proof['ingress'],build=build,protected=protected,installed_workers=source/'engine',
        selected_manifest=source/'manifest.json',
        previous_fixture=deployment/'.q4t-work/serve-live-20261121/fixture-business-01')
    for name in profile_acceptance.PROFILES:
        phase = out/name
        plan = pkg.read(phase/'plan.json')
        previous = phase/'previous-profile.json'
        shutil.copyfile(source/('profile-'+name+'.json'),previous)
        prior = pkg.sha(previous)
        pkg.require(prior==reuse.ASSISTANTS_REVIEWED[name]==
                    manifest['recovery_profiles']['qualifications'][name]['evidence_sha256'],
                    'selected package differs from the qualified predecessor')
        ingress = phase/'engine/q4t-ingress'
        shutil.copy2(plan['ingress'],ingress)
        policy = pkg.read(phase/'client-policy.json')
        owner = next(c for c in policy['clients'] if c['id']=='alpha')
        owner.update(requests_per_minute=60,tokens_per_minute=32000,max_prompt_tokens=8192)
        pkg.save(phase/'client-policy.json',policy)
        shutil.copy2(phase/'client-policy.json',phase/'engine/client-policy.json')
        plan.update(ingress=str(ingress),skill_handoff=dict(previous=prior,release=published,
                    protocol=protocol(sources),binary=new_engine),
                    client_policy_sha256=pkg.sha(phase/'client-policy.json'),
                    installed_policy_sha256=pkg.sha(phase/'engine/client-policy.json'))
        for path in (*ACTORS.values(), Path(__file__), PROOF, RELEASE):
            plan['sources'][str(path.relative_to(ROOT))] = pkg.sha(path)
        pkg.save(phase/'plan.json',plan)


def business(stage,capabilities):
    name = stage.plan['name']
    shared = dict(base=f"https://127.0.0.1:{stage.plan['public_port']}/v1",key=stage.keys['alpha'],
        other=stage.keys['beta'],sdk=stage.plan['sdk_directory'],source=str(RELEASE),
        source_sha256=pkg.sha(RELEASE),phase='create' if name=='primary' else 'resume')
    previous = pkg.read(stage.out.parent/'primary/skill-handoff.json') if name=='light' else {}
    result = {}
    for key,actor in ACTORS.items():
        params = dict(shared)
        if previous: params['prior'] = previous[key]
        if key=='budget': params['max_prompt_tokens']=BUDGET
        run = subprocess.run(['node',actor],input=json.dumps(params),text=True,capture_output=True,
            timeout=100,env=dict(os.environ,NODE_EXTRA_CA_CERTS=stage.plan['tls']['certificate']))
        (stage.out/(key+'-handoff.stderr')).write_text(run.stderr)
        pkg.require(run.returncode==0,f'{name} {key} SDK handoff failed')
        result[key]=json.loads(run.stdout)
    pkg.save(stage.out/'skill-handoff.json',result)
    verify(stage.out,stage.plan,capabilities)


def verify(phase,plan,capabilities):
    name = plan['name']
    change = plan['skill_handoff']
    previous = phase/'previous-profile.json'
    prior = profile_record.read(previous)
    declared = dict(capabilities)
    proof = pkg.read(PROOF)
    pub = pkg.read(RELEASE)['publication']
    pkg.require(pkg.sha(previous)==change['previous']==reuse.ASSISTANTS_REVIEWED[name] and
                change['release']==plan['selected_sha256']==pub['primary'] and
                pkg.same_json(declared.pop('container_workspaces',None),reuse.SKILLS_CAPABILITY) and
                declared.pop('protocol_source_sha256')==change['protocol'] and
                pkg.same_json(declared,{k:v for k,v in prior['capabilities'].items() if k!='protocol_source_sha256'}) and
                plan['capacity']==prior['capacity'] and plan['binary_sha256']==change['binary'] and
                plan['ingress_sha256']==proof['ingress'], 'workspace profile or retained capability drift')
    rows=pkg.read(phase/'skill-handoff.json')
    skill,run=rows['skill'],rows['budget']
    pkg.require(all(row['sdk']==proof['sdk'] and row['revision']==pub['publication_revision'] and
                    row['source_sha256']==pkg.sha(RELEASE) for row in (skill,run)) and
                skill['foreign_status']==[404,404] and run['foreign_status']==404 and
                all(skill['ids'][key].startswith(prefix) for key,prefix in
                    [('skill','skill_'),('version_id','skillver_'),('container','cntr_'),('file','cfile_')]) and
                all(profile_hash(h) for h in (skill['v1'],skill['v2'])) and skill['v1']!=skill['v2'] and
                run['max_prompt_tokens']==BUDGET and json.loads(run['arguments'])=={} and
                all(run[k] for k in ('assistant','thread','run','step','call')), 'invalid SDK handoff record')
    if name=='primary':
        pkg.require(skill['phase']=='create' and not skill['deleted'] and run['status']=='requires_action'
                    and run['events'][-1]=='thread.run.requires_action','primary did not persist its inputs')
    else:
        before=pkg.read(phase.parent/'primary/skill-handoff.json')
        pkg.require(all(skill[k]==before['skill'][k] for k in ('ids','v1','v2','source_sha256')) and
                    skill['phase']=='resume' and skill['deleted'] and
                    all(run[k]==before['budget'][k] for k in ('assistant','thread','run','step','call','arguments','max_prompt_tokens')) and
                    run['status']=='completed' and run['answer']==str(pub['publication_revision']) and
                    0<run['prompt_tokens']<=BUDGET and run['total_tokens']>=run['prompt_tokens'] and
                    run['events'][-1]=='thread.run.completed' and run['events'].count('thread.run.step.completed')==2,
                    'light profile lost stored content, the pending call or its budget')
    evidence=prior['business']['live']['evidence_sha256']
    pkg.require(COMMON <= evidence.keys() and 'assistant-handoff.json' in evidence,'missing prior profile observations')
    return dict({k:v for k,v in evidence.items() if k not in COMMON},
                **{'skill-handoff.json':pkg.sha(phase/'skill-handoff.json')})


def profile_hash(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('build','output','deployment'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    out=args.output.resolve()
    pkg.require(out.is_relative_to(ROOT/'.q4t-work'),'private evidence directory required')
    prepare(out,args.build.resolve(strict=True),args.deployment.resolve(strict=True))
