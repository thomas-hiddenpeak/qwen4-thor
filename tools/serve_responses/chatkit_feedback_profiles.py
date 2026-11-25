"""Qualify ChatKit browser state and SDK history across service capacities."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses.assistant_profiles import COMMON
from tools.serve_service import package

ROOT = lifecycle.ROOT
PROOF = ROOT / 'docs/evidence/serve-chatkit-20261125/feedback.json'
RELEASE = ROOT / 'docs/evidence/serve-evals-20261125/meteor-delivery.json'
ACTOR = Path(__file__).with_name('chatkit_feedback_sdk.mjs').resolve()
VALIDATOR = ROOT / 'tools/serve_audit/sdk_wire_receipt.py'
RECEIPT = 'chatkit-feedback-handoff.json'
KEY = 'chatkit_feedback_handoff'


def prepare(out, build, deployment, official):
    proof = package.read(PROOF)
    package.require(proof['device']['status'] == 'PASS'
                    and len(proof['management_methods']) == 6
                    and package.sha(official) == proof['sdk']['wire']['spec_sha256'],
                    'official schema or ChatKit HTTP proof differs')
    sources = (ACTOR, VALIDATOR, Path(__file__).resolve(), Path(lifecycle.__file__), PROOF, RELEASE)
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.METEOR_REVIEWED, KEY, sources)
    for name in ('primary', 'light'):
        path = out / name / 'plan.json'
        plan = package.read(path)
        plan[KEY]['official'] = str(official)
        package.save(path, plan)


def stored_votes(directory, row):
    thread = row['thread']
    package.require(re.fullmatch(r'cthr_[0-9a-f]{32}', thread), 'invalid thread identity')
    record, = directory.glob('*.chatkit_thread.'+thread+'.json')
    stored = package.read(record)['record']
    data = stored['data']
    expected = dict(zip(row['items'], row['expected_votes'], strict=True))
    tag = data['items'][0]['content'][-1]
    package.require(stored['id'] == thread and stored['kind'] == 'chatkit_thread'
                    and data['feedback'] == expected and tag['type'] == 'input_tag'
                    and tag['id'] == row['entity_id'] and '@'+tag['text'] == row['sdk_mention'],
                    'stored votes or browser tag differ')
    return package.sha(record)


def exercise(out, params, env, objects):
    previous = params.get('previous')
    prior_sha = stored_votes(objects, previous) if previous else None
    with (out/'chatkit-sdk.stderr').open('xb') as err:
        result = subprocess.run(['node', ACTOR], input=json.dumps(params), text=True,
                                stdout=subprocess.PIPE, stderr=err, timeout=140, env=env)
    package.require(result.returncode == 0, 'ChatKit browser or SDK verification failed')
    row = json.loads(result.stdout)
    row['store_sha256'] = stored_votes(objects, row)
    if previous:
        row['previous_store_sha256'] = prior_sha
    return row


def business(stage, capabilities):
    plan = stage.plan
    official = plan[KEY]['official']
    package.require(package.sha(official) == package.read(PROOF)['sdk']['wire']['spec_sha256'],
                    'official schema identity changed')
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1", key=stage.keys['alpha'],
                  other=stage.keys['beta'], sdk=plan['sdk_directory'], model='qwen3.8-flash-next',
                  python=sys.executable, official=official,
                  revision=package.read(RELEASE)['publication']['publication_revision'])
    if plan['name'] == 'light':
        params['previous'] = package.read(stage.out.parent/'primary'/RECEIPT)
    directory = Path(plan['command'][plan['command'].index('--object-store-dir')+1])
    env = dict(os.environ, NODE_EXTRA_CA_CERTS=plan['tls']['certificate'])
    row = exercise(stage.out, params, env, directory)
    package.save(stage.out/RECEIPT, row)
    verify(stage.out, plan, capabilities)


def check_result(row, first, *, restored):
    answer = str(package.read(RELEASE)['publication']['publication_revision'])
    package.require(row['sdk'] == '7.47.0' and row['model_answer'] == answer
                    and row['sdk_mention'] == '@rev'+answer and row['browser_tag']
                    and row['public_rows_identical'] and len(set(row['items'])) == 2
                    and [row[k] for k in ('sibling','peer','missing_item','revoked','repeat')]
                    == [404,404,404,401,200] and row['store_sha256']
                    and row['wire']['spec_sha256'] == package.read(PROOF)['sdk']['wire']['spec_sha256'],
                    'feedback, mention, client isolation or official wire checks failed')
    if restored:
        package.require(row['previous_store_sha256'] == first['store_sha256']
                        and row['continued'] == answer and row['expected_votes'] == ['negative','positive']
                        and all(row[k] == first[k] for k in ('workflow','thread','items','entity_id')),
                        'restored browser state differs from the original')
    else:
        package.require(row['expected_votes'] == ['positive','negative'], 'unexpected original votes')


def verify(phase, plan, capabilities):
    prior = profile_record.read(phase/'previous-profile.json')
    change, name = plan[KEY], plan['name']
    expected = dict(prior['capabilities'], protocol_source_sha256=change['protocol'],
                    chatkit=reuse.FEEDBACK_CHATKIT)
    package.require(package.sha(phase/'previous-profile.json') == change['previous']
                    == reuse.METEOR_REVIEWED[name] and prior['capacity'] == plan['capacity']
                    and package.same_json(expected, capabilities) and plan['selected_sha256']
                    == change['release'] == package.read(RELEASE)['publication']['primary'],
                    'browser qualification requires the selected issued capacity')
    row = package.read(phase/RECEIPT)
    restored = name == 'light'
    first = package.read(phase.parent/'primary'/RECEIPT) if restored else row
    check_result(row, first, restored=restored)
    inherited = prior['business']['live']['evidence_sha256']
    package.require(COMMON <= inherited.keys() and 'eval-meteor-handoff.json' in inherited,
                    'missing issued evaluation qualification')
    return {**{k:v for k,v in inherited.items() if k not in COMMON}, RECEIPT:package.sha(phase/RECEIPT)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('output','build','deployment','official'):
        parser.add_argument('--'+key, required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    package.require(output.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory required')
    prepare(output, args.build.resolve(strict=True), args.deployment.resolve(strict=True),
            args.official.resolve(strict=True))
