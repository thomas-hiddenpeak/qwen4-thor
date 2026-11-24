"""Qualify a partially scored Evals run across the two issued capacities."""
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
RELEASE = ROOT/'docs/evidence/serve-chatkit-20261125/delivery.json'
PROOF = ROOT/'docs/evidence/serve-evals-20261125/model-graders.json'
SCOPE = ROOT/'docs/evidence/serve-scope-decision-20261120/decision.json'
ACTOR = ROOT/'tools/serve_responses/eval_handoff_sdk.mjs'
RECEIPT = 'eval-handoff.json'


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    package.require(proof['device']['status'] == 'PASS' and
                    proof['device']['usage']['invocation_count'] == 4,
                    'missing native model grading evidence')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.CHATKIT_REVIEWED,
                                'eval_handoff', (ACTOR, Path(__file__), Path(lifecycle.__file__),
                                SCOPE, PROOF, RELEASE))


def capture_stopped_run(phase):
    """Observe persisted progress between the cleaned primary and unstarted light."""
    first = package.read(phase.parent/'primary'/RECEIPT)
    cleaned = package.read(phase.parent/'primary'/'result.json')
    package.require(cleaned['status'] == 'PASS' and cleaned['default_unchanged'],
                    'primary has not exited cleanly')
    root = phase.parent/'state/application-objects'
    matches = list(root.glob('*.evalrun.' + first['run'] + '.json'))
    package.require(len(matches) == 1, 'expected one original evaluation run')
    record = package.read(matches[0])['record']
    if isinstance(record, str):
        record = json.loads(record)
    data = record['data']
    resource = data['resource']
    expected = [row['id'] for row in first['items']]
    ids = data['output_ids']
    package.require(resource['id'] == first['run'] and resource['eval_id'] == first['evaluation']
                    and resource['status'] == 'in_progress' and len(expected) <= len(ids) < 6
                    and ids[:len(expected)] == expected and data['row'] == len(ids),
                    'primary must leave real unfinished model work')
    package.save(phase/'eval-stopped.json', dict(run=resource['id'], items=ids, total=data['row']))


def business(stage, capabilities):
    plan = stage.plan
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1",
                  key=stage.keys['alpha'], other=stage.keys['beta'],
                  sdk=plan['sdk_directory'], phase=plan['name'])
    if plan['name'] == 'light':
        params['previous'] = package.read(stage.out.parent/'primary'/RECEIPT)
    env = dict(os.environ, NODE_EXTRA_CA_CERTS=plan['tls']['certificate'])
    with (stage.out/'eval-handoff.stderr').open('xb') as err:
        result = subprocess.run(['node', ACTOR], input=json.dumps(params), text=True,
                                stdout=subprocess.PIPE, stderr=err, timeout=120, env=env)
    package.require(result.returncode == 0, 'Evals handoff failed for '+plan['name'])
    row = json.loads(result.stdout)
    if plan['name'] == 'light':
        row['stopped'] = package.read(stage.out/'eval-stopped.json')
    package.save(stage.out/RECEIPT, row)
    verify(stage.out, plan, capabilities)


def verify(phase, plan, capabilities):
    old = profile_record.read(phase/'previous-profile.json')
    change = plan['eval_handoff']
    name = plan['name']
    expected = dict(old['capabilities'], protocol_source_sha256=change['protocol'],
                    evals=reuse.EVAL_CAPABILITY)
    package.require(package.sha(phase/'previous-profile.json') == change['previous'] ==
                    reuse.CHATKIT_REVIEWED[name] and old['capacity'] == plan['capacity'] and
                    package.same_json(expected, capabilities) and plan['selected_sha256'] ==
                    change['release'] == package.read(RELEASE)['publication']['primary'],
                    'Evals profile or predecessor differs')
    row = package.read(phase/RECEIPT)
    first = row if name == 'primary' else package.read(phase.parent/'primary'/RECEIPT)
    answer = str(package.read(SCOPE)['counts']['required_inference_or_application_service'])
    package.require(row['phase'] == name and row['sdk'] == '7.47.0' and
                    row['source_deleted'] == row['peer'] == 404 and
                    row['source_sha256'] == package.sha(SCOPE) and
                    first['status'] == 'in_progress' and 0 < len(first['items']) < 6 and
                    all(row[k] == first[k] for k in ('evaluation', 'run', 'file')) and
                    row['items'][:len(first['items'])] == first['items'] and
                    len({r['id'] for r in row['items']}) == len(row['items']) and
                    all(r['answer'] == answer and r['index'] == i and r['score'] == 1 - i % 2
                        for i,r in enumerate(row['items'])), 'invalid Evals progress or isolation')
    if name == 'light':
        stopped = package.read(phase/'eval-stopped.json')
        ids = [item['id'] for item in row['items']]
        package.require(row['stopped'] == stopped and stopped['run'] == row['run']
                        and 0 < stopped['total'] < len(ids)
                        and ids[:stopped['total']] == stopped['items'],
                        'persisted progress does not match final output IDs')
        package.require(row['status'] == 'completed' and row['chat'] == '42' and
                        row['counts'] == dict(total=6, passed=3, failed=3, errored=0) and
                        len(row['items']) == row['usage'][0]['invocation_count'] == 6,
                        'original model-sampled run did not finish')
    inherited = old['business']['live']['evidence_sha256']
    package.require(COMMON <= inherited.keys() and 'chatkit-handoff.json' in inherited,
                    'missing issued ChatKit evidence')
    return {**{k:v for k,v in inherited.items() if k not in COMMON}, RECEIPT:package.sha(phase/RECEIPT)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('output', 'build', 'deployment'):
        parser.add_argument('--'+key, required=True, type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    package.require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory required')
    prepare(out, args.build.resolve(strict=True), args.deployment.resolve(strict=True))
