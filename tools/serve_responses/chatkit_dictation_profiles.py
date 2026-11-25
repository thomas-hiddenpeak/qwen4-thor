"""Qualify actual ChatKit dictation and a cancelled Eval across service capacities."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses.assistant_profiles import COMMON
from tools.serve_service import package

ROOT = lifecycle.ROOT
FIXTURES = ROOT / 'docs/evidence/serve-chatkit-composer-20261125'
PROOF = FIXTURES / 'business.json'
RELEASE = ROOT / 'docs/evidence/serve-assistant-pagination-20261125/delivery.json'
ACTOR = Path(__file__).with_name('chatkit_transcription_sdk.mjs')
KEY = 'chatkit_dictation_handoff'
RECEIPT = reuse.COMPOSER_RECEIPT


def recordings():
    fixtures = package.read(PROOF)['fixtures']
    files = {kind: FIXTURES / ('query.' + kind) for kind in ('wav', 'webm')}
    package.require(all(package.sha(path) == fixtures[k] for k, path in files.items()),
                    'recording differs from the verified input')
    return files


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    package.require(proof['result']['status'] == 'PASS' and proof['result']['unchanged_default'],
                    'missing GPU dictation proof')
    sources = [PROOF, RELEASE, ACTOR, Path(__file__), Path(lifecycle.__file__), *recordings().values()]
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.PAGINATION_REVIEWED, KEY, sources)


def check_result(row, previous=None):
    fixtures = package.read(PROOF)['fixtures']
    clips = ('wav', 'webm')
    expected = fixtures['expected']
    package.require(row['sdk'] == '7.47.0' and row['independent'] == '42' and
                    row['transcripts'] == dict.fromkeys(clips, expected) and
                    row['inputs'] == {k: fixtures[k] for k in clips}, 'recorded speech or Chat changed')
    checks = row['observations']
    package.require([r['status'] for r in checks] == [400, 415, 401, 200, 200, 401]
                    and all(r['method'] == 'POST' and r['path'] == '/v1/chatkit' for r in checks)
                    and checks[3]['body_sha256'] == checks[4]['body_sha256']
                    == package.read(PROOF)['sdk']['observations'][3]['body_sha256'],
                    'browser dictation or session isolation failed')
    run = row['canceled_run']
    package.require(re.fullmatch(r'eval_[0-9a-f]{32}', run['eval_id']) and
                    re.fullmatch(r'evalrun_[0-9a-f]{32}', run['run_id']) and
                    re.fullmatch(r'[0-9a-f]{64}', run['sha256']) and
                    run['status'] == 'canceled' and run['foreign_status'] == 404,
                    'missing canceled Eval or tenant isolation')
    if previous is not None:
        package.require(run == previous['canceled_run'], 'recovered Eval changed identity or content')


def exercise(out, params, env, previous=None):
    with (out / 'dictation-sdk.stderr').open('xb') as err:
        run = subprocess.run(['node', ACTOR], input=json.dumps(dict(params, handoff=True,
                             previous=previous['canceled_run'] if previous else None,
                             **{k: str(v) for k, v in recordings().items()})), text=True,
                             stdout=subprocess.PIPE, stderr=err, timeout=160, env=env)
    package.require(run.returncode == 0, 'dictation or Eval client failed')
    row = json.loads(run.stdout)
    check_result(row, previous)
    return row


def business(stage, capabilities):
    plan = stage.plan
    previous = package.read(stage.out.parent/'primary'/RECEIPT) if plan['name'] == 'light' else None
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1", key=stage.keys['alpha'],
                  other=stage.keys['beta'], sdk=plan['sdk_directory'], model='qwen3.8-flash-next')
    row = exercise(stage.out, params, dict(os.environ, NODE_EXTRA_CA_CERTS=plan['tls']['certificate']),
                   previous)
    package.save(stage.out/RECEIPT, row)
    verify(stage.out, plan, capabilities)


def verify(phase, plan, capabilities):
    change, name = plan[KEY], plan['name']
    path = phase / 'previous-profile.json'
    prior = profile_record.read(path)
    expected = dict(prior['capabilities'], protocol_source_sha256=change['protocol'],
                    chatkit=reuse.composer_capability(prior['capabilities']))
    package.require(package.sha(path) == change['previous'] == reuse.PAGINATION_REVIEWED[name]
                    and change['release'] == plan['selected_sha256'] == package.read(RELEASE)['publication']['primary']
                    and prior['capacity'] == plan['capacity'] and package.same_json(expected, capabilities)
                    and expected['chatkit']['input_transcribe']['configured'],
                    'issued recovery capacity or declared ASR changed')
    previous = package.read(phase.parent/'primary'/RECEIPT) if name == 'light' else None
    check_result(package.read(phase/RECEIPT), previous)
    inherited = prior['business']['live']['evidence_sha256']
    package.require(COMMON <= inherited.keys() and reuse.PAGINATION_RECEIPT in inherited,
                    'missing issued Assistant pagination evidence')
    return {**{k:v for k,v in inherited.items() if k not in COMMON}, RECEIPT:package.sha(phase/RECEIPT)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('output', 'build', 'deployment'):
        parser.add_argument('--'+key, required=True, type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    package.require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory required')
    prepare(out, args.build.resolve(strict=True), args.deployment.resolve(strict=True))
