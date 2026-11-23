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
from tools.serve_responses import session_profile_acceptance as sessions
from tools.serve_responses import environment_profile_acceptance as environments
from tools.serve_responses import vault_profile_acceptance as vaults
from tools.serve_responses import interruption_profile_acceptance as recovery
from tools.serve_responses.beta_profile_acceptance import protocol
from tools.serve_service import package as pkg

EVIDENCE = ROOT/'docs/evidence/serve-agent-registration-20261123/checks.json'
SDK = 'tools/serve_responses/agent_registration_sdk.mjs'
CAPABILITY = dict(selector='agents=v1', endpoint='/v1/agents',
    operations=['create','retrieve','update','list','delete'],
    storage='authenticated_client', retention='until_deleted', tool_list_max_bytes=3145728)


def prepare(out, build, original, session_witness=None, qualify_vaults=False, vault_oauth=False,
            environment_templates=False, hosted_workspaces=False):
    environment_templates = environment_templates or hosted_workspaces
    vault_oauth = vault_oauth or environment_templates
    receipt = pkg.read(EVIDENCE)
    deployed = ROOT/'build/service'
    delivery = pkg.read(recovery.EVIDENCE.parent/'delivery.json')['publication']
    if session_witness is not None:
        delivery = pkg.read(sessions.PUBLICATION)['publication']
    if qualify_vaults or vault_oauth:
        pkg.require(session_witness is None, 'select a single candidate source')
        publication = vaults.OAUTH_PUBLICATION if vault_oauth else vaults.PUBLICATION
        if environment_templates:
            publication = (environments.workspace_release(environments.workspace_proof(pkg.sha(build/'q4t')))
                           if hosted_workspaces else environments.PUBLICATION)
        delivery = pkg.read(publication)['publication']
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
    session = sessions.candidate(build, session_witness, current) if session_witness else None
    vault = None
    if environment_templates:
        vault = environments.candidate(build, current, hosted_workspaces)
    elif qualify_vaults or vault_oauth:
        vault = vaults.candidate(build, current, vault_oauth)
    selection = vault or session
    ingress = selection['ingress'] if selection else receipt['ingress_sha256']
    if session:
        change.update(sdk_binary=pkg.sha(session_witness/'q4t'), sdk_protocol=session['tested_protocol'])
    elif not vault:
        pkg.require(change['sdk_binary'] == receipt['engine_sha256'], 'registration SDK binary drift')
    pkg.require(protocol({n:old[n] for n in current if n in old}) == engine['protocol_sha256'] and
                all(pkg.sha(ROOT/n) == digest for n,digest in current.items()) and
                pkg.sha(ROOT/SDK) == receipt['sdk_driver_sha256'], 'SDK/build/current source binding')
    for filename, digest in pkg.read(recovery.EVIDENCE)['original']['record'].items():
        pkg.require(pkg.sha(original/filename) == digest, 'interrupted state fixture differs')
    protected = {str(ROOT/'build'/n): pkg.sha(ROOT/'build'/n) for n in (
        'q4t','q4t.release.json','service/manifest.json','service.release.json','service-native-state/ledger.json')}
    profile_acceptance.prepare_profiles(out,manifest,engine,
        dict(engine['live_workers'],engine_sha256=pkg.sha(build/'q4t')), ingress,
        protected=protected, build=build, beta_change=change, agent_change=dict(
            witness_sha256=pkg.sha(agents.EVIDENCE),
            capabilities=pkg.read(agents.EVIDENCE)['selected']['capabilities']))
    fixture = vaults.prepare_peer(out, vault_oauth) if vault else None
    for name in profile_acceptance.PROFILES:
        phase = out/name
        path = phase/'plan.json'
        plan = pkg.read(path)
        # The build directory may be reused by later work. Keep the actual
        # ingress with this stage so offline review can rehash the tested bytes.
        pinned = phase/'engine/q4t-ingress'
        shutil.copy2(plan['ingress'],pinned)
        pkg.require(pkg.sha(pinned) == ingress, 'ingress snapshot drift')
        plan.update(ingress=str(pinned), agent_registration=pkg.sha(EVIDENCE),
                    agent_interruption=dict(original=str(original), evidence_sha256=pkg.sha(recovery.EVIDENCE)))
        for source in (recovery.SDK, SDK, 'tools/serve_responses/interruption_profile_acceptance.py',
                       'tools/serve_responses/registration_profile_acceptance.py'):
            plan['sources'][source] = pkg.sha(ROOT/source)
        if vault:
            plan['agent_vaults'] = dict(vault, fixture=fixture)
            if vault.get('subagents'):
                # Keep the original quota/rejection exercise on its own owner.
                policy = pkg.read(phase/'client-policy.json')
                gamma = next(client for client in policy['clients'] if client['id'] == 'gamma')
                gamma.update(requests_per_minute=12, tokens_per_minute=32000, max_prompt_tokens=8192)
                pkg.save(phase/'client-policy.json', policy)
                installed = Path(plan['binary']).parent/'client-policy.json'
                shutil.copy2(phase/'client-policy.json', installed)
                plan.update(client_policy_sha256=pkg.sha(phase/'client-policy.json'),
                            installed_policy_sha256=pkg.sha(installed))
            if vault.get('session_tools'):
                indices = 'tools/serve_responses/agent_session_output_indices.mjs'
                plan['sources'][indices] = pkg.sha(ROOT/indices)
            plan['command'].extend(['--mcp-policy-file',str(out/'mcp/policy.json')])
            sources = [vaults.SDK, vaults.HELPER, vaults.PEER, str(publication.relative_to(ROOT))]
            if vault_oauth:
                sources.append(vaults.OAUTH_ISSUER)
            if environment_templates:
                sources.extend(environments.SOURCES)
                if hosted_workspaces:
                    proof = environments.workspace_proof(vault['binary'])
                    sources.extend(environments.workspace_sources(proof))
            for source in sources:
                plan['sources'][source] = pkg.sha(ROOT/source)
        if selection:
            plan['agent_sessions'] = selection
            for source in (sessions.SDK, sessions.HELPER, str(sessions.PUBLICATION.relative_to(ROOT))):
                plan['sources'][source] = pkg.sha(ROOT/source)
        pkg.save(path,plan)


def verify(phase, plan, capabilities):
    expected = pkg.read(EVIDENCE)
    if 'agent_sessions' in plan:
        sessions.verify_identity(plan, capabilities)
    else:
        pkg.require(plan['binary_sha256'] == expected['engine_sha256'] and
                    plan['ingress_sha256'] == expected['ingress_sha256'], 'registration actor identity')
    pkg.require(plan.get('agent_registration') == pkg.sha(EVIDENCE) and
                capabilities.get('agent_registration') == CAPABILITY and
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
    if result.returncode:
        (stage.out/'registration.stderr').write_text(result.stderr)
        raise RuntimeError('official registration SDK failed in '+name+'; see private registration.stderr')
    observed = json.loads(result.stdout)
    if name=='light':
        observed.update(id=params['id'],second=params['second'])
    pkg.save(stage.out/'registration.json',observed)
    verify(stage.out,stage.plan,capabilities)


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    for opt in ('output','build','original'):
        cli.add_argument('--'+opt,type=Path,required=True)
    cli.add_argument('--session-witness', type=Path)
    source = cli.add_mutually_exclusive_group()
    source.add_argument('--vaults', action='store_true')
    source.add_argument('--vault-oauth', action='store_true')
    source.add_argument('--environment-templates', action='store_true')
    source.add_argument('--hosted-workspaces', action='store_true')
    args = cli.parse_args()
    out = args.output.resolve()
    pkg.require(out.is_relative_to(ROOT/'.q4t-work'), 'private stage required')
    prepare(out,args.build.resolve(strict=True),args.original.resolve(strict=True),
            args.session_witness.resolve(strict=True) if args.session_witness else None,
            args.vaults, args.vault_oauth, args.environment_templates, args.hosted_workspaces)
