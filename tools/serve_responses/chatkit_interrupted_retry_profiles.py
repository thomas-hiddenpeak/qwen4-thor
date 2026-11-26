"""Retry an interrupted document review after the original model has stopped."""
import hashlib
import json
from pathlib import Path
import re

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

KEY = 'chatkit_interrupted_retry_handoff'
ACTOR = Path(__file__).with_name('chatkit_generation_profile_sdk.mjs').resolve()
PROOF = lifecycle.ROOT / 'docs/evidence/serve-chatkit-interrupted-retry-20261127/business.json'
RELEASE = lifecycle.ROOT / 'docs/evidence/serve-video-track-grid-20261127/delivery.json'


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    retry = proof['sdk']['retried']
    package.require(proof['stage'] == 'native_tls_verified' and proof['run']['status'] == 'PASS'
                    and proof['product'] == dict(engine=package.sha(build/'q4t'),
                                                  ingress=package.sha(build/'q4t-ingress'))
                    and retry['source_deleted'] == 404
                    and retry['canonical'] == proof['retry']['binding']['item_id']
                    and retry['interruptedSha256'] == proof['interrupted']['journal_sha256'],
                    'missing verified interrupted input, deleted file or accepted product')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.GRID_REVIEWED, KEY,
                                [PROOF, RELEASE, ACTOR, Path(__file__), Path(lifecycle.__file__)])


def document(release):
    publication = release['publication']
    fields = dict(publication_revision=publication['publication_revision'],
                  previous_revision=release['previous_revision'], desired=publication['desired'],
                  primary=publication['primary'], fallback=publication['fallback'])
    data = json.dumps(fields, ensure_ascii=False, separators=(',', ':')).encode()
    return str(fields['publication_revision']), hashlib.sha256(data).hexdigest()


def check(row, previous=None):
    revision, digest = document(package.read(RELEASE))
    source = row['source']
    original = source['interrupted']
    binding = original['binding']
    require = package.require
    typed = lambda value, prefix: re.fullmatch(prefix + '[0-9a-f]{32}', value or '')
    sha = lambda value: re.fullmatch('[0-9a-f]{64}', value or '')
    require(row['sdk'] == '7.47.0' and row['chat'] == '42' and row['revision'] == revision
            and source['document_sha256'] == digest and source['deleted'] == 404
            and source['received'].strip() and source['partial'].startswith(source['received'])
            and all(typed(source[key], prefix) for key, prefix in (
                ('workflow','agent_'), ('thread','cthr_'), ('file','file_'),
                ('item','msg_'), ('user','item_'))),
            'original ChatKit grant, file, partial answer or control differs')
    require(original['status'] == 'cancelled' and original['published'] is False
            and original['file'] == source['file'] and typed(original['response'], 'resp_')
            and sha(original['record_sha256']) and binding['path'] == '/content/0'
            and typed(binding['item_id'], 'item_') and sha(binding['binding_sha256'])
            and typed(binding['snapshot']['id'], 'input_snapshot_') and sha(binding['snapshot']['sha256']),
            'original cancelled input, scoped snapshot or item publication differs')
    if previous is None:
        require(row['phase'] == 'primary' and 'recovered' not in row,
                'primary must leave the original interrupted turn pending retry')
        return

    check(previous)
    result = row['recovered']
    saved = result['replacement']
    ids = result['ids']
    require(row['phase'] == 'light' and package.same_json(source, previous['source'])
            and package.same_json(result['original'], dict(original, published=True))
            and saved['status'] == 'completed' and saved['published'] is True
            and saved['file'] == source['file'] and package.same_json(saved['binding'], binding)
            and typed(saved['response'], 'resp_') and saved['response'] != original['response']
            and sha(saved['record_sha256']) and saved['record_sha256'] != original['record_sha256']
            and len(ids) == 2 and ids[0] == source['user'] and typed(ids[1], 'msg_')
            and ids[1] != source['item'] and re.match(re.escape(revision) + r'\b', result['text'])
            and result['peer'] == result['sibling'] == result['deleted'] == 404,
            'recovered response, original snapshot, visible history, isolation or answer differs')
