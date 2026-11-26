"""Qualify native Chat frames and stored H.264 responses across managed capacities."""
from pathlib import Path
import re
import shutil

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

KEY = 'visual_frame_handoff'
RECEIPT = 'visual-frame-handoff.json'
ROOT = lifecycle.ROOT
ACTOR = ROOT / 'tools/serve_responses/visual_frames_sdk.mjs'
PROOF = ROOT / 'docs/evidence/serve-visual-frame-parts-20261126/business.json'
RELEASE = ROOT / 'docs/evidence/serve-visual-answer-encoding-20261126/delivery.json'
CLIP = ROOT / 'tests/fixtures/file_video/transition.mp4'


def prepare(out, build, deployment, image_dir):
    proof = package.read(PROOF)
    package.require(proof['stage'] == 'targeted_native_tls_sdk_passed'
                    and proof['engine'] == package.sha(build / 'q4t')
                    and proof['ingress'] == package.sha(build / 'q4t-ingress')
                    and proof['node_driver_sha256'] == package.sha(ACTOR)
                    and proof['source_video_sha256'] == package.sha(CLIP),
                    'frame qualification requires the tested engine, client and clip')
    images = proof['source_images']
    package.require(set(images) == {'green', 'blue'} and all(
        digest == package.sha(image_dir / f'solid-{color}.png')
        for color, digest in images.items()), 'qualification source images changed')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.VISUAL_ANSWER_REVIEWED,
                                KEY, [PROOF, RELEASE, ACTOR, CLIP, Path(__file__)])
    for color in images:
        shutil.copyfile(image_dir / f'solid-{color}.png', out / f'solid-{color}.png')


def sdk_parameters(stage):
    proof = package.read(PROOF)
    images = []
    for color, digest in proof['source_images'].items():
        path = stage.out.parent / f'solid-{color}.png'
        package.require(package.sha(path) == digest, 'qualification image changed')
        images.append(dict(path=str(path), color=color, sha256=digest))
    package.require(package.sha(CLIP) == proof['source_video_sha256'], 'qualification clip changed')
    return dict(images=images, clip=dict(path=str(CLIP), sha256=proof['source_video_sha256']))


def color_word(text, color):
    return isinstance(text, str) and re.fullmatch(color + r'[.!]?', text.strip(), re.IGNORECASE)


def check(row, previous=None):
    proof = package.read(PROOF)
    expected = proof['sdk']
    video = row['file_video']
    parts = video['parts']
    streamed = row['chat_untyped_image']
    previews = row['previews']
    package.require(row['sdk'] == expected['sdk'] and row['passed'] is True
                    and row['controls'] == expected['controls']
                    and all(value is True for value in row['controls'].values())
                    and row['invalid_video_url'] == 400 and row['independent'].strip() == '42'
                    and not row['chat_video'].get('refusal') and color_word(row['chat_video']['content'], 'green')
                    and streamed['finish'] == 'stop' and not streamed['refusal'] and color_word(streamed['text'], 'blue'),
                    'ordinary Chat frame, image stream or control did not complete')
    package.require(len(previews) == 3 and previews[0] == previews[1]
                    and all(item['guidance'] is True and item['visual_tokens'] > 0 and
                            item['prompt_tokens'] > 0 for item in previews)
                    and video['peer'] == 404 and video['sha256'] == proof['source_video_sha256']
                    and re.fullmatch(r'resp_[0-9a-f]{32}', video['id'])
                    and len(parts) == 1 and parts[0]['type'] == 'output_text'
                    and color_word(parts[0]['text'], 'blue'),
                    'video preflight, original clip, typed final color or owner mismatch')
    if previous is None:
        package.require(row['restored'] is None, 'first capacity claimed an earlier video')
        return
    check(previous)
    old = previous['file_video']
    package.require(video['id'] != old['id'] and row['restored'] == dict(
        id=old['id'], parts=old['parts'], peer=404), 'original video identity, content or owner changed')
