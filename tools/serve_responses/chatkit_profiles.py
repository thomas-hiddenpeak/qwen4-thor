"""Verify resume of one ChatKit thread, its document and pending tool across capacities."""
import argparse
import json
import os
from pathlib import Path
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses.assistant_profiles import COMMON
from tools.serve_service import package

ROOT = lifecycle.ROOT
RELEASE = ROOT / 'docs/evidence/serve-webhooks-20261124/preparation-delivery.json'
SCOPE = ROOT / 'docs/evidence/serve-scope-decision-20261120/decision.json'
PROOF = ROOT / 'docs/evidence/serve-chatkit-20261125/attachments.json'
ACTOR = ROOT / 'tools/serve_responses/chatkit_handoff_sdk.mjs'
RECEIPT = 'chatkit-handoff.json'


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    package.require(proof['status'] == 'ACCEPT_NATIVE_CHATKIT_DIRECT_DOCUMENT_UPLOAD'
                    and proof['business']['source_sha256'] == package.sha(SCOPE)
                    and proof['business']['source_deleted'] == 404, 'missing ChatKit document proof')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.PREPARATION_REVIEWED,
                                'chatkit_handoff', (ACTOR, Path(__file__), Path(lifecycle.__file__),
                                SCOPE, PROOF, RELEASE))


def business(stage, capabilities):
    plan = stage.plan
    name = plan['name']
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1", key=stage.keys['alpha'],
                  other=stage.keys['beta'], sdk=plan['sdk_directory'], phase=name)
    if name == 'light':
        params['previous'] = package.read(stage.out.parent/'primary'/RECEIPT)
    env = dict(os.environ, NODE_EXTRA_CA_CERTS=plan['tls']['certificate'])
    with (stage.out/'chatkit-handoff.stderr').open('xb') as err:
        result = subprocess.run(['node', ACTOR], input=json.dumps(params), stdout=subprocess.PIPE,
                                stderr=err, text=True, timeout=120, env=env)
    package.require(result.returncode == 0, 'ChatKit handoff failed for '+name)
    package.save(stage.out/RECEIPT, json.loads(result.stdout))
    verify(stage.out, plan, capabilities)


def verify(phase, plan, capabilities):
    prior_path = phase/'previous-profile.json'
    old = profile_record.read(prior_path)
    change = plan['chatkit_handoff']
    expected = dict(old['capabilities'], protocol_source_sha256=change['protocol'],
                    chatkit=reuse.CHATKIT_CAPABILITY)
    package.require(package.same_json(expected, capabilities) and plan['capacity'] == old['capacity']
                    and package.sha(prior_path) == change['previous'] == reuse.PREPARATION_REVIEWED[plan['name']]
                    and package.read(RELEASE)['publication']['primary'] == change['release'] == plan['selected_sha256'],
                    'ChatKit capacity, capabilities or predecessor mismatch')
    row = package.read(phase/RECEIPT)
    package.require(row['sdk'] == '7.47.0' and row['phase'] == plan['name'] and row['peer'] == 404,
                    'missing real SDK or tenant boundary')
    first = row if row['phase'] == 'primary' else package.read(phase.parent/'primary'/RECEIPT)
    package.require(first['bytes'] == SCOPE.stat().st_size and first['sha256'] == package.sha(SCOPE)
                    and first['proposed_count'] == package.read(SCOPE)['counts']['required_inference_or_application_service']
                    and first['source_deleted'] == 404 and all(isinstance(first[k],str) and
                    first[k].startswith(prefix) for k,prefix in dict(thread='cthr_', workflow='agent_',
                    attachment='file_', call='call_', session='cksess_').items()), 'missing original pending tool or file')
    if row['phase'] == 'light':
        expected_answer = str(package.read(SCOPE)['counts']['required_inference_or_application_service'])
        package.require(row['thread'] == first['thread'] and row['call'] == first['call']
                        and row['attachment'] == first['attachment'] and row['session'] != first['session']
                        and row['other_user'] == 404 and row['repeat'] == 409 and row['chat'] == '42'
                        and row['verified_count'] == first['proposed_count'] and row['answer'] == expected_answer and row['items'] == 3,
                        'original user, tool, document context or healthy peer was lost')
    inherited = old['business']['live']['evidence_sha256']
    package.require(COMMON <= inherited.keys() and lifecycle.PREPARATION_RECEIPT in inherited,
                    'missing predecessor profile evidence')
    return {**{k:v for k,v in inherited.items() if k not in COMMON}, RECEIPT:package.sha(phase/RECEIPT)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('output','build','deployment'):
        parser.add_argument('--'+key,type=Path,required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    package.require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory required')
    prepare(out, args.build.resolve(strict=True), args.deployment.resolve(strict=True))
