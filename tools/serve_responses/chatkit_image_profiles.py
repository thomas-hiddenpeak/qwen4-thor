"""Qualify a durable image history and a real uploaded file across serving profiles."""
from pathlib import Path
import re

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import chatkit_workflow_profiles as workflows
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

PROOF = lifecycle.ROOT / 'docs/evidence/serve-chatkit-image-attachments-20261126/reserved-previews.json'
HISTORY = PROOF.with_name('history.json')
RELEASE = lifecycle.ROOT / 'docs/evidence/serve-chatkit-reasoning-workflow-20261126/delivery.json'
ACTOR = Path(__file__).with_name('chatkit_image_profile_sdk.mjs')
IMAGE = lifecycle.ROOT / 'tests/fixtures/file_images/original.png'
KEY = 'chatkit_image_handoff'
RECEIPT = 'chatkit-image-handoff.json'


def prepare(out, build, deployment, source):
    final = package.read(PROOF)['native_https'][-1]
    history = package.read(HISTORY)
    completed = PROOF.with_name('completed-previews.json')
    package.require(final['serving']['binary'] == package.sha(build/'q4t')
                    and final['ingress_sha256'] == package.sha(build/'q4t-ingress')
                    and final['result']['status'] == 'PASS' and final['result']['cleanup_ok']
                    and package.sha(IMAGE) == history['fresh_source_sha256']
                    and package.sha(completed) == history['completed_preview_receipt_sha256'],
                    'unverified image candidate, fixture or source history')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.CHATKIT_WORKFLOW_REVIEWED,
                                KEY, [PROOF, HISTORY, completed, RELEASE, IMAGE, ACTOR, Path(__file__),
                                      Path(workflows.__file__), Path(lifecycle.__file__)])
    workflows.restore_reviewed_thread(out/'state', source, history)


def check(row, previous=None):
    h = package.read(HISTORY)
    expected = {key:h[key] for key in ('thread', 'item', 'file', 'items', 'preview_sha256')}
    package.require(row['sdk'] == '7.47.0' and row['chat'] == '42' and row['archive'] == expected
                    and row['source_deleted'] == row['sibling'] == row['peer'] == 404,
                    'historical image, isolation or independent chat changed')
    upload = row['upload']
    package.require(re.fullmatch(r'file_[0-9a-f]{32}', upload['file']) and upload['file'] != h['file']
                    and upload['source_sha256'] == h['fresh_source_sha256']
                    and re.fullmatch('[0-9a-f]{64}', upload['preview_sha256'])
                    and upload['before'] == 404 and upload['read'] == 200 and upload['peer'] == 404,
                    'live reservation or file identity changed')
    if previous is None:
        package.require(row['phase'] == 'primary' and 'recovered' not in row, 'invalid primary handoff')
    else:
        check(previous)
        recovered = row['recovered']
        package.require(row['phase'] == 'light' and upload['file'] != previous['upload']['file']
                        and upload['preview_sha256'] == previous['upload']['preview_sha256']
                        and recovered == dict(file=previous['upload']['file'], source_sha256=h['fresh_source_sha256'],
                                              peer=404, after_delete=404),
                        'original uploaded bytes or scope did not survive profile replacement')
