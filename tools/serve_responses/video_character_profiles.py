"""Check reference clip persistence across the issued service capacities."""

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
FIXTURES = ROOT / 'docs/evidence/serve-video-characters-20261125'
PROOF = FIXTURES / 'business.json'
CLIP = FIXTURES / 'moving-robot.mp4'
RELEASE = ROOT / 'docs/evidence/serve-chatkit-composer-20261125/delivery.json'
ACTOR = Path(__file__).with_name('video_character_sdk.mjs')
KEY = 'video_character_handoff'
RECEIPT = 'video-character-handoff.json'


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    package.require(proof['run']['status'] == 'PASS' and proof['run']['unchanged_default']
                    and proof['stored']['verified_against_original']
                    and package.sha(CLIP) == proof['stored']['sha256'],
                    'missing original video reference proof')
    sources = [PROOF, CLIP, RELEASE, ACTOR, Path(__file__), Path(lifecycle.__file__)]
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.COMPOSER_REVIEWED, KEY, sources)


def check_result(row, proof, previous=None):
    resource = row['resource']
    correct = proof['sdk']['resource']
    package.require(isinstance(resource, dict) and set(resource) == {'id', 'created_at', 'name'}
                    and re.fullmatch(r'char_[a-f0-9]{32}', resource['id'])
                    and type(resource['created_at']) is int and resource['created_at'] > 0
                    and resource['name'] == correct['name'], 'invalid video character resource')
    package.require(row['sdk'] == '7.47.0' and row['independent'] == '42'
                    and row['clip_sha256'] == proof['stored']['sha256']
                    and package.same_json(row['capability'], proof['sdk']['capability'])
                    and [row['foreign'], row['corrupt'], row['long_name']] == [404, 400, 400]
                    and row['lookup'] == dict(function='get_video_character', argument_id=resource['id'],
                                              resolved={'name': resource['name']}),
                    'character lookup, access controls or independent Chat failed')
    paths = [('post', '/videos/characters'), ('get', '/videos/characters/{character_id}')]
    if previous is not None:
        package.require(package.same_json(resource, previous['resource']),
                        'restored character differs from the original reference')
        paths = paths[1:]
    package.require([(r['method'], r['path']) for r in row['wire']] == paths
                    and all(r['status'] == 200 and package.same_json(r['value'], resource)
                            for r in row['wire']), 'invalid character response sequence')


def exercise(out, params, env, previous=None, *, clip=CLIP, proof=PROOF):
    payload = dict(params, clip=str(clip), previous=previous['resource'] if previous else None)
    with (out/'character-sdk.stderr').open('xb') as err:
        run = subprocess.run(['node', ACTOR], input=json.dumps(payload), text=True,
                             stdout=subprocess.PIPE, stderr=err, timeout=160, env=env)
    package.require(run.returncode == 0, 'video character client failed')
    result = json.loads(run.stdout)
    check_result(result, package.read(proof), previous)
    return result


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
    name, change = plan['name'], plan[KEY]
    path = phase/'previous-profile.json'
    prior = profile_record.read(path)
    proof = package.read(PROOF)
    expected = dict(prior['capabilities'], protocol_source_sha256=change['protocol'],
                    video_characters=proof['sdk']['capability'])
    package.require(package.sha(path) == change['previous'] == reuse.COMPOSER_REVIEWED[name]
                    and change['release'] == plan['selected_sha256'] == package.read(RELEASE)['publication']['primary']
                    and prior['capacity'] == plan['capacity'] and package.same_json(expected, capabilities),
                    'issued capacity, video reference contract or predecessor changed')
    previous = package.read(phase.parent/'primary'/RECEIPT) if name == 'light' else None
    check_result(package.read(phase/RECEIPT), proof, previous)
    inherited = prior['business']['live']['evidence_sha256']
    package.require(COMMON <= inherited.keys() and reuse.COMPOSER_RECEIPT in inherited,
                    'missing issued browser dictation handoff')
    return {**{k:v for k,v in inherited.items() if k not in COMMON}, RECEIPT:package.sha(phase/RECEIPT)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('output', 'build', 'deployment'):
        parser.add_argument('--'+key, required=True, type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    package.require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory required')
    prepare(out, args.build.resolve(strict=True), args.deployment.resolve(strict=True))
