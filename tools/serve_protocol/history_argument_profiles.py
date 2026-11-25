"""Recover a stored rejected call and its corrected tool result across capacities."""
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
PROOF = ROOT/'docs/evidence/serve-chat-history-arguments-20261125/business.json'
RELEASE = ROOT/'docs/evidence/serve-project-safety-20261125/delivery.json'
SCOPE = ROOT/'docs/evidence/serve-scope-decision-20261120/decision.json'
ACTOR = Path(__file__).with_name('history_argument_sdk.mjs').resolve()
RECEIPT = 'history-arguments-handoff.json'
KEY = 'history_arguments_handoff'


def prepare(out, build, deployment):
    package.require(package.read(PROOF)['device']['status'] == 'PASS',
                    'missing real Chat and Responses history qualification')
    sources = (ACTOR, Path(__file__).resolve(), Path(lifecycle.__file__), PROOF, RELEASE, SCOPE)
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.SAFETY_REVIEWED, KEY, sources)


def exercise(out, params, env):
    with (out/'history-sdk.stderr').open('xb') as err:
        result = subprocess.run(['node', ACTOR], input=json.dumps(params), text=True,
                                stdout=subprocess.PIPE, stderr=err, timeout=130, env=env)
    package.require(result.returncode == 0, 'tool history SDK did not finish')
    return json.loads(result.stdout)


def business(stage, capabilities):
    plan = stage.plan
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1", key=stage.keys['alpha'],
                  other=stage.keys['beta'], sdk=plan['sdk_directory'], phase=plan['name'],
                  model='qwen3.8-flash-next')
    if params['phase'] == 'light':
        params['previous'] = package.read(stage.out.parent/'primary'/RECEIPT)
    row = exercise(stage.out, params, dict(os.environ, NODE_EXTRA_CA_CERTS=plan['tls']['certificate']))
    package.save(stage.out/RECEIPT, row)
    verify(stage.out, plan, capabilities)


def check_result(row, first, *, restored):
    package.require(row['sdk'] == '7.47.0' and row['peer_status'] == 404
                    and row['original_text'] == '{"record":' and row['stored']
                    and row['source_call'] == 'call_previous'
                    and row['corrected_call'] and row['corrected_call'] != row['source_call']
                    and row['corrected_arguments'] == {'record': 'scope'}
                    and package.same_json(row['capability'], reuse.REPAIRED_TOOL_HISTORY),
                    'SDK history bytes, corrected call or tenant scope differs')
    if restored:
        expected = str(package.read(SCOPE)['counts']['required_inference_or_application_service'])
        package.require(all(row[k] == value for k, value in first.items())
                        and row['answer'] == row['response_answer'] == expected
                        and row['response'] and row['terminal'] == 'stop',
                        'recovered tool arguments, application result or SSE terminal differs')


def verify(phase, plan, capabilities):
    prior = profile_record.read(phase/'previous-profile.json')
    change = plan[KEY]
    old = prior['capabilities']
    expected = dict(old, protocol_source_sha256=change['protocol'],
                    tools=dict(old['tools'], history=reuse.REPAIRED_TOOL_HISTORY),
                    template=dict(old['template'],
                                  renderer_source_sha256=package.sha(ROOT/'src/server/chat_template.cpp'),
                                  tool_history_source_sha256=package.sha(ROOT/'src/server/chat_tool_history.cpp')))
    name = plan['name']
    package.require(package.sha(phase/'previous-profile.json') == change['previous']
                    == reuse.SAFETY_REVIEWED[name] and prior['capacity'] == plan['capacity']
                    and package.same_json(old['tools']['history'], reuse.ISSUED_TOOL_HISTORY)
                    and package.same_json(expected, capabilities) and plan['selected_sha256']
                    == change['release'] == package.read(RELEASE)['publication']['primary'],
                    'tool history requires the issued capacity and reviewed contract')
    row = package.read(phase/RECEIPT)
    restored = name == 'light'
    first = package.read(phase.parent/'primary'/RECEIPT) if restored else row
    check_result(row, first, restored=restored)
    inherited = prior['business']['live']['evidence_sha256']
    package.require(COMMON <= inherited.keys() and 'project-safety-handoff.json' in inherited,
                    'missing issued project safety qualification')
    return {**{k:v for k,v in inherited.items() if k not in COMMON}, RECEIPT: package.sha(phase/RECEIPT)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('output', 'build', 'deployment'):
        parser.add_argument('--'+key, required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    package.require(output.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory required')
    prepare(output, args.build.resolve(strict=True), args.deployment.resolve(strict=True))
