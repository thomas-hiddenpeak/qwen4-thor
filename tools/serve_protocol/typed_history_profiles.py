"""Qualify typed function-call history and its stored messages across capacities."""
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
PROOF = ROOT/'docs/evidence/serve-history-argument-types-20261125/business.json'
RELEASE = ROOT/'docs/evidence/serve-video-track-selection-20261125/delivery.json'
SCOPE = ROOT/'docs/evidence/serve-scope-decision-20261120/decision.json'
ACTOR = Path(__file__).with_name('typed_history_sdk.mjs').resolve()
KEY = 'typed_history_handoff'
RECEIPT = reuse.TYPED_HISTORY_RECEIPT


def check_result(row, previous=None):
    count = package.read(SCOPE)['counts']['required_inference_or_application_service']
    cases = row['cases']
    package.require(row['sdk'] == '7.47.0' and row['source_paths'] == count
                    and row['protected_peer'] == 404 and len(cases) == 2
                    and len({item['id'] for item in cases}) == 2,
                    'missing independent typed conversations or tenant protection')
    for index, (value, kind) in enumerate([(None, 'null'), ('null', 'string')]):
        case = cases[index]
        package.require(case['expected'] == kind and json.loads(case['arguments']) == {'selector':value}
                        and package.same_json(case['answer'], {'selector_type':kind, 'required_paths':count})
                        and case['terminal'] == 'stop' and case['prompt_tokens'] > 0
                        and case['id'] and case['call_id'], 'typed function history or retrieved answer differs')
        if previous is not None:
            old = previous['cases'][index]
            package.require(case.get('recovered_from') == old['id'] and case['id'] != old['id']
                            and all(case[k] == old[k] for k in ('call_id', 'arguments', 'expected', 'answer')),
                            'history reload did not retain the original call and result')


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    package.require(proof['run']['status'] == 'PASS' and proof['run']['unchanged_default']
                    and proof['driver_sha256'] == package.sha(ACTOR), 'missing real typed history SDK run')
    check_result(proof['sdk'])
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.TRACK_REVIEWED, KEY,
                                [PROOF, SCOPE, RELEASE, ACTOR, Path(__file__), Path(lifecycle.__file__)])


def verify(phase, plan, capabilities):
    prior_file = phase/'previous-profile.json'
    prior = profile_record.read(prior_file)
    change = plan[KEY]
    name = plan['name']
    old = prior['capabilities']
    expected = dict(old, protocol_source_sha256=change['protocol'],
                    template=dict(old['template'], renderer_source_sha256=package.sha(ROOT/'src/server/chat_template.cpp')))
    package.require(package.sha(prior_file) == change['previous'] == reuse.TRACK_REVIEWED[name]
                    and plan['selected_sha256'] == change['release'] == package.read(RELEASE)['publication']['primary']
                    and prior['capacity'] == plan['capacity'] and package.same_json(expected, capabilities),
                    'typed history changed the issued capacity or another contract')
    previous = package.read(phase.parent/'primary'/RECEIPT) if name == 'light' else None
    check_result(package.read(phase/RECEIPT), previous)
    inherited = prior['business']['live']['evidence_sha256']
    package.require(COMMON <= inherited.keys() and reuse.TRACK_RECEIPT in inherited,
                    'missing issued dual-track recovery evidence')
    return {**{k:v for k,v in inherited.items() if k not in COMMON}, RECEIPT:package.sha(phase/RECEIPT)}


def business(stage, capabilities):
    plan = stage.plan
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1", key=stage.keys['alpha'],
                  other=stage.keys['beta'], sdk=plan['sdk_directory'], model='qwen3.8-flash-next')
    if plan['name'] == 'light':
        params['previous'] = package.read(stage.out.parent/'primary'/RECEIPT)
    with (stage.out/'typed-history-sdk.stderr').open('xb') as err:
        run = subprocess.run(['node', ACTOR], input=json.dumps(params), text=True, stdout=subprocess.PIPE,
                             stderr=err, timeout=150, env=dict(os.environ, NODE_EXTRA_CA_CERTS=plan['tls']['certificate']))
    package.require(run.returncode == 0, 'typed history SDK failed; see typed-history-sdk.stderr')
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
