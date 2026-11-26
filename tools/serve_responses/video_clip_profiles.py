"""Verify the same deleted-file video history across both qualified capacities."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

ROOT = lifecycle.ROOT
KEY = 'video_clip_handoff'
RECEIPT = 'video-clip-handoff.json'
ACTOR = Path(__file__).with_name('video_clip_sdk.py')
FIXTURE = ROOT / 'tests/fixtures/file_video/rect-clips.mov'
PROOF = ROOT / 'docs/evidence/serve-video-clipping-20261126/repaired-business.json'
RELEASE = ROOT / 'docs/evidence/serve-responses-errors-20261126/delivery.json'
REGION_KEY = 'quickdraw_region_handoff'
REGION_PROOF = ROOT / 'docs/evidence/serve-video-regions-20261126/business.json'
REGION_RELEASE = PROOF.with_name('delivery.json')
REGION_FIXTURE = FIXTURE.with_name('region-clips.mov')
ID = re.compile(r'resp_[0-9a-f]{32}')
DIGEST = re.compile(r'[0-9a-f]{64}')


def prepare(out, build, deployment, interpreter, *, regions=False):
    proof_path, fixture, key, release, reviewed = (
        (REGION_PROOF, REGION_FIXTURE, REGION_KEY, REGION_RELEASE, reuse.VIDEO_CLIP_REVIEWED)
        if regions else (PROOF, FIXTURE, KEY, RELEASE, reuse.RESPONSE_FAILURE_REVIEWED))
    proof = package.read(proof_path)
    sdk = subprocess.check_output([interpreter, '-IB', '-c', 'import openai; print(openai.__version__)'],
                                  text=True, timeout=15).strip()
    package.require(sdk == proof['sdk']['sdk'] and proof['device']['status'] == 'PASS'
                    and proof['engine'] == package.sha(build/'q4t')
                    and proof['ingress'] == package.sha(build/'q4t-ingress'),
                    'candidate differs from the accepted video upload')
    lifecycle.prepare_candidate(out, build, deployment, release, reviewed,
                                key, [ACTOR, fixture, proof_path, release, Path(__file__)])
    for name in ('primary', 'light'):
        path = out/name/'plan.json'
        plan = package.read(path)
        command = plan['command']
        package.require(Path(command[command.index('--object-store-dir')+1]) ==
                        out/'state/application-objects', 'capacities must share the isolated object store')
        plan[key]['interpreter'] = str(Path(interpreter).absolute())
        package.save(path, plan)


def check(row, previous=None, *, regions=False):
    proof = package.read(REGION_PROOF if regions else PROOF)['sdk']
    replies = row['responses']
    expected = {'uploaded','inline','replay'}
    if previous is not None:
        expected.add('restored')
    package.require(set(replies) == expected and row['sources'] == proof['sources'] and
                    row['sdk'] == proof['sdk'] and row['foreign'] == 404 and row['malformed'] == 400
                    and row['deleted_before_replay'] is True and row['independent_chat'] == '42',
                    'video handoff scope, source or ordinary Chat differs')
    identifiers = set()
    for label, reply in replies.items():
        colors = ({'top_left':'red'} if label == 'uploaded' else
                  proof['responses']['inline' if label == 'inline' else 'replay']['colors'])
        rid = reply['response']
        package.require(ID.fullmatch(rid) and rid not in identifiers and reply['status'] == 'completed'
                        and reply['colors'] == colors and type(reply['input_tokens']) is int
                        and reply['input_tokens'] > 0, 'invalid clip identity, colors or outcome')
        identifiers.add(rid)
        if label != 'uploaded':
            events = reply['events']
            package.require(events and all(type(idx) is int and idx == i for i,(idx,_) in enumerate(events))
                            and events[0][1] == 'response.created' and events[-1][1] == 'response.completed'
                            and sum(t == 'response.completed' for _,t in events) == 1
                            and any(t == 'response.output_text.delta' for _,t in events),
                            'clipped video stream sequence or terminal differs')
    original = row['saved']
    package.require(row['counted'] == replies['uploaded']['input_tokens'] and
                    original['response'] == replies['uploaded']['response'] and
                    re.fullmatch(r'file_[0-9a-f]{32}', original['file']) and
                    DIGEST.fullmatch(original['resource_sha256']) and original['get'] == 200
                    and original['deleted'] == original['foreign'] == 404,
                    'earlier result must survive deletion and remain owner scoped')
    if previous is None:
        package.require('restored' not in row, 'unexpected prior video response')
        return
    check(previous, regions=regions)
    package.require(row['restored'] == previous['saved']
                    and not identifiers.intersection(v['response'] for v in previous['responses'].values())
                    and original['file'] != previous['saved']['file'],
                    'handoff lost the actual prior video, file or GET bytes')


def check_regions(row, previous=None):
    check(row, previous, regions=True)


def exercise(params, interpreter, errors, captures, previous=None, *, regions=False):
    command = dict(params, handoff=True, fixture=str(REGION_FIXTURE if regions else FIXTURE),
                   capture=str(captures), quickdraw_region=regions)
    if previous is not None:
        command['previous'] = previous['saved']
    proc = subprocess.run([interpreter, '-IB', ACTOR], input=json.dumps(command), text=True,
                          stdout=subprocess.PIPE, stderr=errors, timeout=150, check=True)
    row = json.loads(proc.stdout)
    check(row, previous, regions=regions)
    for name, source in [('saved',row['saved']),('restored',row.get('restored'))]:
        if source is not None:
            raw = (captures/(name+'-get.json')).read_bytes()
            package.require(hashlib.sha256(raw).hexdigest() == source['resource_sha256']
                            and json.loads(raw)['id'] == source['response'],
                            'captured resource differs from the scoped GET')
    return row
