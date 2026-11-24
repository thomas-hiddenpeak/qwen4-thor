"""Qualify stored interpreter outputs and active-container reuse across capacities."""
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
PROOF = ROOT/'docs/evidence/serve-code-interpreter-20261124/business.json'
RELEASE = ROOT/'docs/evidence/serve-skills-20261124/delivery.json'
ACTOR = ROOT/'tools/serve_responses/code_handoff_sdk.mjs'
RECEIPT = 'code-handoff.json'


def prepare(out, build, deployment):
    proof = pkg.read(PROOF)
    release = pkg.read(RELEASE)['publication']
    source = deployment/'build/service'
    manifest = bundle.verify(source, release['primary'])
    engine = pkg.verify(source/'engine', manifest['engine_manifest_sha256'])
    identity = build/'generated/q4t/server/chat_protocol_sources.json'
    sources = pkg.read(identity)
    pkg.require(proof['status'] == 'ACCEPT_HOSTED_PYTHON_WORKSPACE_HTTP' and
                proof['schema']['declared_and_official_match'] and
                proof['binary'] == pkg.sha(build/'q4t') and
                proof['ingress'] == pkg.sha(build/'q4t-ingress') and
                proof['protocol_sources_sha256'] == pkg.sha(identity) and
                all(pkg.sha(ROOT/name) == digest for name, digest in sources.items()),
                'capacity verification needs the accepted binary and protocol')
    protected = {str(deployment/'build'/name): pkg.sha(deployment/'build'/name)
                 for name in ('q4t', 'q4t.release.json', 'service/manifest.json',
                              'service.release.json', 'service-native-state/ledger.json')}
    profile_acceptance.prepare_profiles(
        out, manifest, engine, dict(engine['live_workers'], engine_sha256=proof['binary']),
        proof['ingress'], build=build, protected=protected, installed_workers=source/'engine',
        selected_manifest=source/'manifest.json',
        previous_fixture=deployment/'.q4t-work/serve-live-20261121/fixture-business-01')
    for name in profile_acceptance.PROFILES:
        phase = out/name
        plan = pkg.read(phase/'plan.json')
        previous = phase/'previous-profile.json'
        shutil.copyfile(source/('profile-'+name+'.json'), previous)
        anchor = pkg.sha(previous)
        pkg.require(anchor == reuse.SKILLS_REVIEWED[name] ==
                    manifest['recovery_profiles']['qualifications'][name]['evidence_sha256'],
                    'installed capacity receipt differs from the selected release')
        ingress = phase/'engine/q4t-ingress'
        shutil.copy2(plan['ingress'], ingress)
        policy = pkg.read(phase/'client-policy.json')
        owner = next(c for c in policy['clients'] if c['id'] == 'alpha')
        owner.update(requests_per_minute=60, tokens_per_minute=32000, max_prompt_tokens=8192)
        pkg.save(phase/'client-policy.json', policy)
        shutil.copy2(phase/'client-policy.json', phase/'engine/client-policy.json')
        plan.update(ingress=str(ingress),
                    code_handoff=dict(previous=anchor, release=release['primary'],
                                      protocol=protocol(sources)),
                    client_policy_sha256=pkg.sha(phase/'client-policy.json'),
                    installed_policy_sha256=pkg.sha(phase/'engine/client-policy.json'))
        for path in (ACTOR, Path(__file__), PROOF, RELEASE):
            plan['sources'][str(path.relative_to(ROOT))] = pkg.sha(path)
        pkg.save(phase/'plan.json', plan)


def business(stage, capabilities):
    plan = stage.plan
    create = plan['name'] == 'primary'
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1",
                  key=stage.keys['alpha'], other=stage.keys['beta'], sdk=plan['sdk_directory'],
                  source=str(RELEASE), source_sha256=pkg.sha(RELEASE),
                  phase='create' if create else 'resume')
    if not create:
        params['prior'] = pkg.read(stage.out.parent/'primary'/RECEIPT)
    run = subprocess.run(['node', ACTOR], input=json.dumps(params), capture_output=True, text=True,
                         timeout=105, env=dict(os.environ, NODE_EXTRA_CA_CERTS=plan['tls']['certificate']))
    (stage.out/'code-handoff.stderr').write_text(run.stderr)
    pkg.require(run.returncode == 0, 'workspace SDK handoff failed: ' + plan['name'])
    pkg.save(stage.out/RECEIPT, json.loads(run.stdout))
    verify(stage.out, plan, capabilities)


def verify(phase, plan, capabilities):
    name = plan['name']
    change = plan['code_handoff']
    previous = phase/'previous-profile.json'
    predecessor = profile_record.read(previous)
    proof = pkg.read(PROOF)
    release = pkg.read(RELEASE)['publication']
    remaining = dict(capabilities)
    pkg.require(pkg.sha(previous) == change['previous'] == reuse.SKILLS_REVIEWED[name] and
                change['release'] == plan['selected_sha256'] == release['primary'] and
                pkg.same_json(remaining.pop('hosted_python', None), reuse.CODE_CAPABILITY) and
                remaining.pop('protocol_source_sha256') == change['protocol'] and
                pkg.same_json(remaining, {k:v for k,v in predecessor['capabilities'].items()
                                          if k != 'protocol_source_sha256'}) and
                plan['binary_sha256'] == proof['binary'] and plan['ingress_sha256'] == proof['ingress']
                and plan['capacity'] == predecessor['capacity'], 'hosted interpreter capacity changed')
    row = pkg.read(phase/RECEIPT)
    create = name == 'primary'
    revision = release['publication_revision']
    pkg.require(row['sdk'] == proof['sdk'] and row['revision'] == revision and
                row['source_sha256'] == pkg.sha(RELEASE) and row['foreign_status'] == 404 and
                row['phase'] == ('create' if create else 'resume') and
                row['stdout'] == str(revision if create else revision + 1) and
                row['filename'] == ('version.txt' if create else 'next.txt') and
                row['source_removed'] is create and row['response'].startswith('resp_') and
                row['container'].startswith('cntr_') and row['file'].startswith('cfile_') and
                row['events'].count('response.code_interpreter_call.completed') == 1 and
                'response.output_text.annotation.added' in row['events'] and
                row['events'][-1] == 'response.completed', 'invalid interpreter handoff')
    if create:
        pkg.require(row['prior_response'] is None and row['prior_file'] is None,
                    'unexpected preceding response')
    else:
        prior = pkg.read(phase.parent/'primary'/RECEIPT)
        pkg.require(row['container'] == prior['container'] and row['prior_response'] == prior['response']
                    and row['prior_file'] == prior['file'] and row['response'] != prior['response']
                    and row['file'] != prior['file'], 'previous interpreter files or response were lost')
    prior_proofs = predecessor['business']['live']['evidence_sha256']
    pkg.require(COMMON <= prior_proofs.keys() and 'skill-handoff.json' in prior_proofs,
                'missing retained profile observations')
    return {**{k:v for k,v in prior_proofs.items() if k not in COMMON}, RECEIPT:pkg.sha(phase/RECEIPT)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('output', 'build', 'deployment'):
        parser.add_argument('--'+key, type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    pkg.require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory required')
    prepare(out, args.build.resolve(strict=True), args.deployment.resolve(strict=True))
