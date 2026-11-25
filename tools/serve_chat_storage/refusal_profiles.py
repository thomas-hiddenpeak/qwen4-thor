"""Qualify exact stored refusal fields and message IDs across capacities."""
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
PROOF = ROOT/'docs/evidence/serve-stored-message-projection-20261125/business.json'
RELEASE = ROOT/'docs/evidence/serve-history-argument-types-20261125/delivery.json'
ACTOR = Path(__file__).with_name('refusal_sdk.mjs').resolve()
KEY = 'stored_refusal_handoff'
RECEIPT = reuse.STORED_REFUSAL_RECEIPT


def check_result(row, previous=None):
    cases = row['cases']
    package.require(row['sdk'] == '7.47.0' and len(cases) == 2 and
                    len({case['id'] for case in cases}) == 2 and
                    len({case['message_id'] for case in cases}) == 2,
                    'missing two independent saved conversations')
    for index, (part, field) in enumerate([
            ('未授权修改项目资料。', '未授权修改项目资料。'),
            ('原始拒答片段。', '单独记录的拒答说明。')]):
        case = cases[index]
        package.require(case['refusal'] == field and case['original_content_parts'] ==
                        [{'type': 'refusal', 'refusal': part}] and case['peer'] == 404
                        and case['recovered'] is (previous is not None)
                        and case['id'] and case['message_id'] and case['last_id'],
                        'stored refusal, original part, tenant or cursor differs')
        if previous is not None:
            original = previous['cases'][index]
            package.require(all(case[key] == original[key] for key in
                                ('id', 'message_id', 'last_id', 'refusal', 'original_content_parts')),
                            'recovery did not read the original stored message')


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    package.require(proof['run']['status'] == 'PASS' and proof['run']['unchanged_default']
                    and proof['driver_sha256'] == package.sha(ACTOR), 'missing real stored refusal SDK run')
    check_result(proof['sdk'])
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.TYPED_HISTORY_REVIEWED, KEY,
                                [PROOF, RELEASE, ACTOR, Path(__file__), Path(lifecycle.__file__)])


def verify(phase, plan, capabilities):
    prior_file = phase/'previous-profile.json'
    prior = profile_record.read(prior_file)
    change = plan[KEY]
    name = plan['name']
    old = prior['capabilities']
    expected = dict(old, protocol_source_sha256=change['protocol'])
    package.require(package.sha(prior_file) == change['previous'] == reuse.TYPED_HISTORY_REVIEWED[name]
                    and plan['selected_sha256'] == change['release'] == package.read(RELEASE)['publication']['primary']
                    and prior['capacity'] == plan['capacity'] and package.same_json(expected, capabilities),
                    'stored refusal changed the issued capacity or another contract')
    previous = package.read(phase.parent/'primary'/RECEIPT) if name == 'light' else None
    check_result(package.read(phase/RECEIPT), previous)
    inherited = prior['business']['live']['evidence_sha256']
    package.require(COMMON <= inherited.keys() and reuse.TYPED_HISTORY_RECEIPT in inherited,
                    'missing issued stored refusal recovery evidence')
    return {**{k:v for k,v in inherited.items() if k not in COMMON}, RECEIPT:package.sha(phase/RECEIPT)}


def business(stage, capabilities):
    plan = stage.plan
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1", key=stage.keys['alpha'],
                  other=stage.keys['beta'], sdk=plan['sdk_directory'], model='qwen3.8-flash-next')
    if plan['name'] == 'light':
        params['previous'] = package.read(stage.out.parent/'primary'/RECEIPT)
    with (stage.out/'stored-refusal-sdk.stderr').open('xb') as err:
        run = subprocess.run(['node', ACTOR], input=json.dumps(params), text=True, stdout=subprocess.PIPE,
                             stderr=err, timeout=150, env=dict(os.environ, NODE_EXTRA_CA_CERTS=plan['tls']['certificate']))
    package.require(run.returncode == 0, 'stored refusal SDK failed; see stored-refusal-sdk.stderr')
    package.save(stage.out/RECEIPT, json.loads(run.stdout))
    verify(stage.out, plan, capabilities)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('output', 'build', 'deployment'):
        parser.add_argument('--'+flag, required=True, type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    package.require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory required')
    prepare(out, args.build.resolve(strict=True), args.deployment.resolve(strict=True))
