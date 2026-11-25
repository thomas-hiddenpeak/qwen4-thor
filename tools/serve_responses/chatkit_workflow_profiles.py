"""Qualify a real stored public summary and a pending tool across model profiles."""
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

ROOT = lifecycle.ROOT
PROOF = ROOT / 'docs/evidence/serve-chatkit-reasoning-workflow-20261126/business.json'
HISTORY = PROOF.with_name('history.json')
RELEASE = ROOT / 'docs/evidence/serve-chatkit-generation-stream-20261125/delivery.json'
ACTOR = Path(__file__).with_name('chatkit_workflow_profile_sdk.mjs')
KEY = 'chatkit_workflow_handoff'


def restore_history(state, source):
    """Restore only the two reviewed application objects into a private, fresh test store."""
    fixture = package.read(HISTORY)
    require = package.require
    require(fixture['wire_sha256'] == package.read(PROOF)['wire']['wire_sha256'] and
            fixture['public_summaries'] > 0, 'unqualified historical source')
    restore_reviewed_thread(state, source, fixture)


def restore_reviewed_thread(state, source, fixture):
    """Copy one reviewed thread and its agent atomically, without old session credentials."""
    require = package.require
    expected = fixture['records']
    require(len(expected) == 3 and '.q4t-format' in expected and
            all(Path(name).name == name for name in expected), 'invalid history file names')
    require(state.is_dir() and not (state / 'application-objects').exists(),
            'history restoration requires a new application object directory')
    with TemporaryDirectory(dir=state) as scratch:
        store = Path(scratch) / 'objects'
        store.mkdir(mode=0o700)
        kinds = set()
        for name, digest in expected.items():
            src = source / name
            require(not src.is_symlink() and src.is_file() and src.stat().st_size < 2**20,
                    'missing reviewed history record')
            data = src.read_bytes()
            path = store / name
            with path.open('xb') as out:
                os.fchmod(out.fileno(), 0o600)
                out.write(data)
            require(package.sha(path) == digest, 'historical object bytes changed')
            if name == '.q4t-format':
                continue
            record = package.read(path)['record']
            kind = record['kind']
            require(kind in ('agent','chatkit_thread') and
                    record['id'] == fixture['agent' if kind == 'agent' else 'thread'] and
                    name == '.'.join((record['owner'], kind, record['id'], 'json')),
                    'unexpected history object or owner')
            kinds.add(kind)
        require(kinds == {'agent','chatkit_thread'}, 'missing reviewed thread or agent')
        store.rename(state / 'application-objects')


def prepare(out, build, deployment, source):
    proof = package.read(PROOF)
    live = proof['live']
    require = package.require
    require(proof['candidate'] == package.sha(build/'q4t') and proof['business_answers_pass']
            and live['generated_positive'] and live['all_match'] and live['stream_task_updated']
            and proof['independent_cleanup']['guard_released'],
            'candidate lacks a verified positive public summary and cleanup evidence')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.CHATKIT_GENERATION_REVIEWED,
                                KEY, [PROOF, HISTORY, RELEASE, ACTOR, Path(__file__), Path(lifecycle.__file__)])
    restore_history(out / 'state', source)


def check(row, previous=None):
    fixture = package.read(HISTORY)
    require = package.require
    release = str(package.read(RELEASE)['publication']['publication_revision'])
    require(row['sdk'] == '7.47.0' and row['chat'] == '42' and row['revision'] == release,
            'unexpected SDK, core chat answer or release')
    require(row['archive'] == dict(thread=fixture['thread'], groups_sha256=fixture['groups_sha256'],
                                  items=fixture['items']), 'historical workflow projection changed')
    source = row['source']
    ids = source['ids']
    groups = source['groups']
    require(source['thread'].startswith('cthr_') and source['thread'] != fixture['thread']
            and source['item'].startswith('fc_') and source['call'].startswith('call_')
            and len(ids) == len(set(ids)) and ids[-1] == source['item'] and ids[0].startswith('item_')
            and groups and [i for i in ids if i.startswith('wf_')] == [g[0] for g in groups]
            and all(re.fullmatch('[0-9a-f]{64}', digest) for _, tasks in groups for digest in tasks)
            and re.fullmatch('[0-9a-f]{64}', source['groups_sha256']), 'invalid pending workflow and tool')
    if previous is None:
        require(row['phase'] == 'primary' and 'recovered' not in row, 'pending call not retained')
        return
    check(previous)
    result = row['recovered']
    final = result['ids']
    require(row['phase'] == 'light' and source == previous['source'] and result['answer'] == release
            and result['before_sha256'] == source['groups_sha256'] and result['call'] == source['call']
            and result['item'] == source['item'] and final[:len(ids)] == ids and len(final) > len(ids)
            and len(final) == len(set(final)) and result['message'].startswith('msg_')
            and result['message'] in final[len(ids):] and result['peer'] == result['sibling'] == 404
            and result['duplicate'] == 409, 'restored task, tool, answer or authorization changed')
