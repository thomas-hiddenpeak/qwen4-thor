"""Check ordinary visual responses and policy refusals across managed capacities."""
import re
import shutil
from pathlib import Path

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses import refusal_acceptance as refusal
from tools.serve_service import package

KEY = 'visual_answer_handoff'
RECEIPT = reuse.VISUAL_ANSWER_RECEIPT
ACTOR = Path(__file__).with_name('visual_answer_sdk.mjs')
PROOF = lifecycle.ROOT / 'docs/evidence/serve-visual-answer-encoding-20261126/nullable-user.json'
RELEASE = lifecycle.ROOT / 'docs/evidence/serve-image-speech-recovery-20261126/delivery.json'


def prepare(out, build, deployment, images):
    proof = package.read(PROOF)
    expected = proof['candidate']
    package.require(proof['stage'] == 'targeted_native_schema_and_sdk_passed'
                    and package.sha(build / 'q4t') == expected['engine']
                    and package.sha(build / 'q4t-ingress') == expected['ingress'],
                    'candidate differs from the tested nullable visual runtime')
    hashes = {row['expected']: row['image_sha256'] for row in proof['sdk']['observations']}
    package.require(set(images) == set(hashes) == {'blue', 'green'}
                    and all(package.sha(path) == hashes[color] for color, path in images.items()),
                    'qualification requires both independently tested PNGs')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.MEDIA_HANDOFF_REVIEWED,
                                KEY, [PROOF, RELEASE, ACTOR, Path(__file__), Path(lifecycle.__file__)])
    for color, path in images.items():
        shutil.copyfile(path, out / (color + '.png'))


def sdk_parameters(stage):
    specs = []
    for row in package.read(PROOF)['sdk']['observations']:
        color = row['expected']
        image = stage.out.parent / (color + '.png')
        package.require(package.sha(image) == row['image_sha256'], 'qualification image changed')
        specs.append(dict(path=str(image), color=color, sha256=row['image_sha256']))
    return dict(images=specs, policy=refusal.POLICY, private_question=refusal.QUESTION,
                marker=refusal.MARKER)


def check(row, previous=None):
    reference = package.read(PROOF)['sdk']
    expected = reference['observations']
    cases = row['observations']
    package.require(row['sdk'] == reference['sdk'] and row['chat'].strip() == '42'
                    and row['null_user_identity'] is True and row['invalid'] == reference['invalid']
                    and len(cases) == len(expected) == 2, 'visual profile controls failed')
    for result, fixture in zip(cases, expected):
        color = fixture['expected']
        content = result['content']
        package.require(result['expected'] == color and result['image_sha256'] == fixture['image_sha256']
                        and re.fullmatch(r'resp_[0-9a-f]{32}', result['id'])
                        and result['peer'] == 404 and result['status'] == 'completed'
                        and result['streamed'] is fixture['streamed'] and result['refusal_delta'] is False
                        and len(content) == 1 and content[0]['type'] == 'output_text'
                        and re.fullmatch(color + r'[.!]?', content[0]['text'].strip(), re.IGNORECASE)
                        and result['streamed_text'] == (content[0]['text'] if fixture['streamed'] else '')
                        and result['passed'] is True, 'visual response content, stream or owner differs')
    denial = row['refusal']
    parts = denial['content']
    ids = [item['id'] for item in [*cases, denial]]
    package.require(denial['passed'] is True and denial['status'] == 'completed'
                    and re.fullmatch(r'resp_[0-9a-f]{32}', denial['id']) and len(set(ids)) == 3
                    and len(parts) == 1 and parts[0]['type'] == 'refusal'
                    and parts[0]['refusal'].strip() and refusal.MARKER not in parts[0]['refusal']
                    and row['passed'] is True, 'policy refusal or response identity differs')
    if previous is None:
        package.require(row['restored'] == [], 'first profile cannot claim a prior response')
        return
    check(previous)
    prior = [*previous['observations'], previous['refusal']]
    package.require(not set(ids).intersection(item['id'] for item in prior)
                    and row['restored'] == [dict(id=item['id'], content=item['content'], peer=404)
                                            for item in prior], 'stored response identity or content changed')
