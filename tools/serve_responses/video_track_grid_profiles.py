"""Qualify the exact original track-grid snapshots through profile and package handoff."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import approval_identifiers_profiles as approvals
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses import video_snapshot_bindings
from tools.serve_service import package

KEY = 'video_track_grid_handoff'
ACTOR = Path(__file__).with_name('video_track_grid_handoff_sdk.py')
PROOF = lifecycle.ROOT / 'docs/evidence/serve-video-track-grid-20261127/business.json'
RELEASE = lifecycle.ROOT / 'docs/evidence/serve-program-forks-20261126/delivery.json'


def prepare(out, build, deployment, interpreter, source):
    proof = package.read(PROOF)
    sdk = subprocess.check_output([interpreter, '-IB', '-c', 'import openai;print(openai.__version__)'],
                                  text=True, timeout=15).strip()
    case = proof['frozen']
    package.require(proof['run']['status'] == 'PASS' and sdk == proof['sdk']['sdk']
                    and [case['width'], case['height']] == [97, 48]
                    and case['extent_fixed16'] == [96 * 65536 + 32768, 48 * 65536]
                    and case['sources'] == proof['sdk']['sources'] and len(case['frames']) == 4
                    and proof['product']['binary'] == package.sha(build/'q4t')
                    and proof['product']['ingress'] == package.sha(build/'q4t-ingress'),
                    'fractional snapshots, SDK or accepted product differs')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.FORK_REVIEWED, KEY,
                                [ACTOR, PROOF, RELEASE, Path(approvals.__file__), Path(video_snapshot_bindings.__file__), Path(__file__)])
    store = out/'state/application-objects'
    approvals.seed(source, store, proof)
    ids = dict(zip(case['sources'], case['snapshots'], strict=True))
    package.require(video_snapshot_bindings.check(store, proof['sdk'], ids) == proof['bindings'],
                    'copied input no longer belongs to its accepted response')
    for name in ('primary', 'light'):
        path = out/name/'plan.json'
        plan = package.read(path)
        command = plan['command']
        package.require(Path(command[command.index('--object-store-dir') + 1]) == store,
                        'the video history must be shared by both profiles')
        plan[KEY]['interpreter'] = str(Path(interpreter).absolute())
        package.save(path, plan)


def check(row, previous=None):
    check_handoff(row, previous, package.read(PROOF)['sdk'], {'top_right':'green', 'bottom_right':'yellow'})


def check_handoff(row, previous, old, colors):
    parent = old['saved']['response']
    source_ids = {label: old['responses'][label]['response'] for label in ('uploaded', 'inline', 'replay')}
    package.require(len(set(source_ids.values())) == 3 and row['history'].keys() == source_ids.keys()
                    and row['sdk'] == old['sdk'] and row['deleted'] == 404
                    and row['colors'] == colors
                    and row['independent_chat'] == '42', 'video source, ownership or answer changed')
    for label, rid in source_ids.items():
        page = row['history'][label]
        package.require(page['response'] == rid and page['sha256'] == old['resources'][label]
                        and page['peer'] == 404 and page['previous'] == (parent if label == 'replay' else None),
                        'original resource, parent or owner changed')
    fresh = row['saved']
    events = row['events']
    package.require(re.fullmatch(r'resp_[A-Za-z0-9_-]+', fresh['response'])
                    and fresh['response'] not in source_ids.values() and fresh['previous'] == parent
                    and re.fullmatch(r'[0-9a-f]{64}', fresh['sha256']) and fresh['peer'] == 404
                    and events and events[0] == 'response.created' and events[-1] == 'response.completed'
                    and events.count('response.completed') == 1 and 'response.output_text.delta' in events,
                    'new video continuation or SSE terminal is invalid')
    if previous is None:
        package.require(row['restored'] is None, 'unexpected restored continuation')
    else:
        check_handoff(previous, None, old, colors)
        package.require(row['restored'] == previous['saved'] and row['history'] == previous['history']
                        and fresh['response'] != previous['saved']['response'],
                        'stored video continuation or earlier pictures changed during handoff')


def exercise(params, interpreter, errors, captures, previous=None):
    return run_client(params, interpreter, errors, captures, previous, PROOF, check)


def run_client(params, interpreter, errors, captures, previous, proof, check_result):
    command = dict(params, original=package.read(proof)['sdk'], capture=str(captures))
    if previous is not None:
        command['previous'] = previous['saved']
    run = subprocess.run([interpreter, '-IB', ACTOR], input=json.dumps(command), text=True,
                         stdout=subprocess.PIPE, stderr=errors, timeout=150, check=True)
    row = json.loads(run.stdout)
    check_result(row, previous)
    pages = dict(row['history'], current=row['saved'])
    if previous:
        pages['restored'] = row['restored']
    for label, page in pages.items():
        raw = (captures/(label+'.json')).read_bytes()
        package.require(hashlib.sha256(raw).hexdigest() == page['sha256'] and
                        json.loads(raw)['id'] == page['response'], 'GET bytes differ from the handoff')
    return row
