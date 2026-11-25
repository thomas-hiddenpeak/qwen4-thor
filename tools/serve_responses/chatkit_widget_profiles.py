"""Qualify a release-review widget written on the primary and edited on the light profile."""
from pathlib import Path
import re

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

PROOF = lifecycle.ROOT / 'docs/evidence/serve-chatkit-widget-stream-20261126/components.json'
RELEASE = lifecycle.ROOT / 'docs/evidence/serve-chatkit-image-attachments-20261126/delivery.json'
ACTOR = Path(__file__).with_name('chatkit_widget_profile_sdk.mjs')
KEY = 'chatkit_widget_handoff'
RECEIPT = 'chatkit-widget-handoff.json'


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    package.require(proof['identity']['binary'] == package.sha(build/'q4t')
                    and proof['ingress'] == package.sha(build/'q4t-ingress')
                    and proof['business']['status'] == 'PASS'
                    and proof['reference']['replayed_committed_events'] == 2,
                    'widget profile requires the tested engine and component replay')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.CHATKIT_IMAGE_REVIEWED,
                                KEY, [PROOF, RELEASE, ACTOR, Path(__file__), Path(lifecycle.__file__)])


def check(row, previous=None):
    revision = str(package.read(RELEASE)['publication']['publication_revision'])
    source = row['source']
    prefixes = dict(workflow='agent_', file='file_', thread='cthr_', user='item_',
                    answer='msg_', widget='item_')
    package.require(set(source) == prefixes.keys() and len(set(source.values())) == len(source)
                    and all(isinstance(source[k], str) and re.fullmatch(p+'[0-9a-f]{32}', source[k])
                            for k, p in prefixes.items()), 'invalid durable review identity')
    package.require(row['sdk'] == '7.47.0' and row['chat'] == '42' and row['revision'] == revision
                    and row['before'] == ['rev'+revision, '待复核'] and row['sibling'] == row['peer'] == 404,
                    'review contents, scope or independent chat changed')
    if previous is None:
        package.require(row['phase'] == 'primary' and 'recovered' not in row, 'invalid primary review')
        return
    check(previous)
    package.require(row['phase'] == 'light' and source == previous['source'] and row['recovered'] ==
                    dict(item=source['widget'], after=['rev'+revision, '已复核'], component='review',
                         duplicate=502, deleted=404), 'restored review mutation or saved state changed')
