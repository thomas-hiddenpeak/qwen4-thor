"""Qualify METEOR scores and tenant-scoped row recovery across capacities."""
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
PROOF = ROOT/'docs/evidence/serve-evals-20261125/meteor.json'
RELEASE = ROOT/'docs/evidence/serve-chat-history-arguments-20261125/delivery.json'
SCOPE = ROOT/'docs/evidence/serve-scope-decision-20261120/decision.json'
ACTOR = Path(__file__).with_name('eval_meteor_sdk.mjs').resolve()
RECEIPT = 'eval-meteor-handoff.json'
KEY = 'eval_meteor_handoff'


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    package.require(proof['device']['status'] == 'PASS' and proof['sdk']['wire_objects'] == 4
                    and package.sha(ROOT/'third_party/wordnet/meteor-3.0.zlib')
                    == proof['corpus']['packed_sha256'], 'missing public METEOR acceptance')
    sources = (ACTOR, Path(__file__).resolve(), Path(lifecycle.__file__), PROOF, RELEASE, SCOPE)
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.HISTORY_REVIEWED, KEY, sources)


def exercise(out, params, env):
    with (out/'meteor-sdk.stderr').open('xb') as err:
        result = subprocess.run(['node', ACTOR], input=json.dumps(params), text=True,
                                stdout=subprocess.PIPE, stderr=err, timeout=130, env=env)
    package.require(result.returncode == 0, 'METEOR SDK did not finish')
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
    answer = str(package.read(SCOPE)['counts']['required_inference_or_application_service'])
    ids = ('eval_id', 'run_id', 'output')
    expected = (53/54, 53/54, 0, 23/27)
    scores = row['scores']
    package.require(row['sdk'] == '7.47.0' and row['foreign_status'] == 404
                    and row['model_answer'] == answer and all(row[k] for k in ids)
                    and len(scores) == len(expected)
                    and all(abs(a-b) < 1e-12 for a,b in zip(scores, expected)),
                    'incorrect METEOR evaluation, score or tenant isolation')
    if restored:
        package.require(row['independent_chat'] == '42'
                        and all(row[k] == first[k] for k in (*ids, 'scores', 'model_answer')),
                        'restored METEOR output differs from committed row')


def verify(phase, plan, capabilities):
    prior = profile_record.read(phase/'previous-profile.json')
    change = plan[KEY]
    name = plan['name']
    expected = dict(prior['capabilities'], protocol_source_sha256=change['protocol'],
                    evals=reuse.METEOR_EVALS)
    package.require(package.sha(phase/'previous-profile.json') == change['previous']
                    == reuse.HISTORY_REVIEWED[name] and prior['capacity'] == plan['capacity']
                    and package.same_json(prior['capabilities']['evals'], reuse.COSINE_EVALS)
                    and package.same_json(expected, capabilities) and plan['selected_sha256']
                    == change['release'] == package.read(RELEASE)['publication']['primary'],
                    'METEOR requires the issued capacity and unchanged predecessor metrics')
    row = package.read(phase/RECEIPT)
    restored = name == 'light'
    first = package.read(phase.parent/'primary'/RECEIPT) if restored else row
    check_result(row, first, restored=restored)
    inherited = prior['business']['live']['evidence_sha256']
    package.require(COMMON <= inherited.keys() and 'history-arguments-handoff.json' in inherited,
                    'missing issued tool history qualification')
    return {**{k:v for k,v in inherited.items() if k not in COMMON}, RECEIPT: package.sha(phase/RECEIPT)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('output', 'build', 'deployment'):
        parser.add_argument('--'+key, required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    package.require(output.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory required')
    prepare(output, args.build.resolve(strict=True), args.deployment.resolve(strict=True))
