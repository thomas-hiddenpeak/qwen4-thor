"""Qualify the official Agents configuration SDK across the two managed capacities."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import profile_acceptance
from tools.serve_recovery import bundle
from tools.serve_responses import agent_profile_acceptance as agents
from tools.serve_responses import interruption_profile_acceptance as recovery
from tools.serve_responses.beta_profile_acceptance import protocol
from tools.serve_service import package as pkg

EVIDENCE = ROOT/'docs/evidence/serve-agent-registration-20261123/checks.json'
SDK = 'tools/serve_responses/agent_registration_sdk.mjs'
CAPABILITY = dict(selector='agents=v1', endpoint='/v1/agents',
    operations=['create','retrieve','update','list','delete'],
    storage='authenticated_client', retention='until_deleted', tool_list_max_bytes=3145728)


def prepare(out, build, original):
    receipt = pkg.read(EVIDENCE)
    deployed = ROOT/'build/service'
    delivery = pkg.read(recovery.EVIDENCE.parent/'delivery.json')['publication']
    previous = delivery['primary']
    manifest = bundle.verify(deployed, previous)
    engine = pkg.verify(deployed/'engine', manifest['engine_manifest_sha256'])
    meta = pkg.read(deployed/'engine/q4t.release.json')
    old_file = Path(meta['tested_source_manifest'])
    pkg.require(pkg.sha(old_file) == meta['tested_source_manifest_sha256'], 'installed source manifest drift')
    old = pkg.read(old_file)
    current = pkg.read(build/'generated/q4t/server/chat_protocol_sources.json')
    change = dict(source_manifest_sha256=previous, previous_protocol=engine['protocol_sha256'],
        sdk_binary=pkg.sha(build/'q4t'), sdk_protocol=protocol(current), protocol=protocol(current))
    pkg.require(protocol({n:old[n] for n in current if n in old}) == engine['protocol_sha256'] and
                all(pkg.sha(ROOT/n) == digest for n,digest in current.items()) and
                change['sdk_binary'] == receipt['engine_sha256'] and
                pkg.sha(ROOT/SDK) == receipt['sdk_driver_sha256'], 'SDK/build/current source binding')
    for filename, digest in pkg.read(recovery.EVIDENCE)['original']['record'].items():
        pkg.require(pkg.sha(original/filename) == digest, 'interrupted state fixture differs')
    protected = {str(ROOT/'build'/n): pkg.sha(ROOT/'build'/n) for n in (
        'q4t','q4t.release.json','service/manifest.json','service.release.json','service-native-state/ledger.json')}
    profile_acceptance.prepare_profiles(out,manifest,engine,
        dict(engine['live_workers'],engine_sha256=change['sdk_binary']), receipt['ingress_sha256'],
        protected=protected, beta_change=change, agent_change=dict(
            witness_sha256=pkg.sha(agents.EVIDENCE),
            capabilities=pkg.read(agents.EVIDENCE)['selected']['capabilities']))
    for name in profile_acceptance.PROFILES:
        phase = out/name
        path = phase/'plan.json'
        plan = pkg.read(path)
        # The build directory may be reused by later work. Keep the actual
        # ingress with this stage so offline review can rehash the tested bytes.
        pinned = phase/'engine/q4t-ingress'
        shutil.copy2(plan['ingress'],pinned)
        pkg.require(pkg.sha(pinned) == receipt['ingress_sha256'], 'ingress snapshot drift')
        plan.update(ingress=str(pinned), agent_registration=pkg.sha(EVIDENCE),
                    agent_interruption=dict(original=str(original), evidence_sha256=pkg.sha(recovery.EVIDENCE)))
        for source in (recovery.SDK, SDK, 'tools/serve_responses/interruption_profile_acceptance.py',
                       'tools/serve_responses/registration_profile_acceptance.py'):
            plan['sources'][source] = pkg.sha(ROOT/source)
        pkg.save(path,plan)


def verify(phase, plan, capabilities):
    expected = pkg.read(EVIDENCE)
    pkg.require(plan.get('agent_registration') == pkg.sha(EVIDENCE) and
                capabilities.get('agent_registration') == CAPABILITY and
                plan['binary_sha256'] == expected['engine_sha256'] and
                plan['ingress_sha256'] == expected['ingress_sha256'] and
                plan['sources'][SDK] == expected['sdk_driver_sha256'], 'registration witness identity')
    row = pkg.read(phase/'registration.json')
    pkg.require(row['sdk'] == expected['sdk']['version'] and row['foreign_status'] == 404,
                'SDK or tenant observation differs')
    if plan['name'] == 'primary':
        pkg.require(row['id'].startswith('agent_') and row['second'].startswith('agent_') and
                    row['second'] != row['id'] and row['pages'] == 2 and row['selector_required'] is True and
                    row['tool_types'] == expected['sdk']['tools'], 'primary registration result differs')
    else:
        created = pkg.read(phase.parent/'primary/registration.json')
        pkg.require(row['id'] == created['id'] and row['second'] == created['second'] and
                    all(row[k] is True for k in ('persisted','updated','deleted','sibling_preserved')),
                    'cross-capacity registration continuity differs')
    return {'registration.json': pkg.sha(phase/'registration.json')}


def business(stage, capabilities):
    name = stage.plan['name']
    params = dict(base='https://127.0.0.1:'+str(stage.plan['public_port'])+'/v1',
        key=stage.keys['alpha'],other=stage.keys['gamma'],sdk=stage.plan['sdk_directory'],
        phase='create' if name=='primary' else 'resume')
    if name=='light':
        prior = pkg.read(stage.out.parent/'primary/registration.json')
        params.update(id=prior['id'],second=prior['second'])
    result = subprocess.run(['node',ROOT/SDK],input=json.dumps(params),text=True,capture_output=True,
        env=dict(os.environ,NODE_EXTRA_CA_CERTS=stage.plan['tls']['certificate']),timeout=60)
    pkg.require(result.returncode==0, 'official registration SDK failed in '+name)
    observed = json.loads(result.stdout)
    if name=='light':
        observed.update(id=params['id'],second=params['second'])
    pkg.save(stage.out/'registration.json',observed)
    verify(stage.out,stage.plan,capabilities)


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    for opt in ('output','build','original'):
        cli.add_argument('--'+opt,type=Path,required=True)
    args = cli.parse_args()
    out = args.output.resolve()
    pkg.require(out.is_relative_to(ROOT/'.q4t-work'), 'private stage required')
    prepare(out,args.build.resolve(strict=True),args.original.resolve(strict=True))
