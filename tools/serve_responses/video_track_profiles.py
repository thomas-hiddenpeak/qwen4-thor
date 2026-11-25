"""Qualify a switched MP4 alternate track at both issued service capacities."""

import argparse
import os
from pathlib import Path

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses import video_character_profiles as characters
from tools.serve_responses.assistant_profiles import COMMON
from tools.serve_service import package

ROOT = lifecycle.ROOT
AREA = ROOT / 'docs/evidence/serve-video-track-selection-20261125'
PROOF = AREA / 'business.json'
CLIP = AREA / 'enabled-second.mp4'
RELEASE = ROOT / 'docs/evidence/serve-video-characters-20261125/delivery.json'
KEY = 'alternate_track_handoff'
RECEIPT = reuse.TRACK_RECEIPT


def verify_source(proof):
    stored = proof['stored']
    original_path = ROOT / 'tests/fixtures/file_video/alternate-tracks.mp4'
    original = original_path.read_bytes()
    uploaded = CLIP.read_bytes()
    offsets = proof['fixture']['changed_bytes']
    package.require(len(original) == len(uploaded) and len(offsets) == 2
                    and offsets == [i for i, (a, b) in enumerate(zip(original, uploaded)) if a != b]
                    and all(i >= 7 and original[i-7:i-3] == b'tkhd' for i in offsets)
                    and uploaded[offsets[0]] == (original[offsets[0]] & 0xfe)
                    and uploaded[offsets[1]] == (original[offsets[1]] | 1),
                    'only the enabled flags of the original two video tracks may differ')
    package.require(proof['run']['status'] == 'PASS' and proof['run']['unchanged_default']
                    and stored['verified_against_original'] and stored['sha256'] == package.sha(CLIP)
                    and (stored['width'], stored['height'], stored['duration_ns']) == (64, 128, 2000000000)
                    and proof['fixture']['source_sha256'] == package.sha(original_path),
                    'expected the qualified two-track original and its shorter rotated presentation')


def prepare(out, build, deployment):
    verify_source(package.read(PROOF))
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.CHARACTER_REVIEWED, KEY, [
        PROOF, CLIP, RELEASE, ROOT / 'tests/fixtures/file_video/alternate-tracks.mp4',
        characters.ACTOR, Path(characters.__file__), Path(lifecycle.__file__), Path(__file__)])


def verify(phase, plan, capabilities):
    name = plan['name']
    prior_file = phase/'previous-profile.json'
    prior = profile_record.read(prior_file)
    change = plan[KEY]
    package.require(package.sha(prior_file) == change['previous'] == reuse.CHARACTER_REVIEWED[name]
                    and plan['selected_sha256'] == change['release'] == package.read(RELEASE)['publication']['primary']
                    and prior['capacity'] == plan['capacity']
                    and package.same_json(dict(prior['capabilities'], protocol_source_sha256=change['protocol']), capabilities),
                    'video track qualification changed the previous capacity or capability contract')
    proof = package.read(PROOF)
    verify_source(proof)
    previous = package.read(phase.parent/'primary'/RECEIPT) if name == 'light' else None
    characters.check_result(package.read(phase/RECEIPT), proof, previous)
    inherited = prior['business']['live']['evidence_sha256']
    package.require(COMMON <= inherited.keys() and reuse.CHARACTER_RECEIPT in inherited,
                    'missing issued video reference handoff')
    return {**{k:v for k,v in inherited.items() if k not in COMMON}, RECEIPT:package.sha(phase/RECEIPT)}


def business(stage, capabilities):
    plan = stage.plan
    prior = package.read(stage.out.parent/'primary'/RECEIPT) if plan['name'] == 'light' else None
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1", key=stage.keys['alpha'],
                  other=stage.keys['beta'], sdk=plan['sdk_directory'], model='qwen3.8-flash-next')
    row = characters.exercise(stage.out, params, dict(os.environ, NODE_EXTRA_CA_CERTS=plan['tls']['certificate']),
                              prior, clip=CLIP, proof=PROOF)
    package.save(stage.out/RECEIPT, row)
    verify(stage.out, plan, capabilities)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('output', 'build', 'deployment'):
        parser.add_argument('--'+flag, required=True, type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    package.require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory required')
    prepare(out, args.build.resolve(strict=True), args.deployment.resolve(strict=True))
