"""Qualify dictionary tool results and optional Agent updates across model lifetimes."""
import json
from pathlib import Path
import re
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

ROOT = lifecycle.ROOT
KEY = 'tool_map_handoff'
ACTOR = Path(__file__).with_name('tool_map_sdk.py')
EVIDENCE = ROOT/'docs/evidence'
PROOF = EVIDENCE/'serve-tool-map-references-20261126/business.json'
RELEASE = EVIDENCE/'serve-video-regions-20261126/delivery.json'
PREVIOUS = EVIDENCE/'serve-video-clipping-20261126/delivery.json'


def prepare(out, build, deployment, interpreter):
    proof = package.read(PROOF)
    sdk = subprocess.check_output([interpreter, '-IB', '-c', 'import openai;print(openai.__version__)'],
                                  text=True, timeout=15).strip()
    package.require(proof['stage'] == 'candidate_native_https' and proof['device']['status'] == 'PASS'
                    and sdk == proof['sdk']['sdk'] and proof['engine'] == package.sha(build/'q4t')
                    and proof['ingress'] == package.sha(build/'q4t-ingress'),
                    'unqualified release lookup or candidate')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.REGION_REVIEWED, KEY,
                                [PROOF, ACTOR, RELEASE, PREVIOUS, Path(__file__)])
    for name in ('primary', 'light'):
        path = out/name/'plan.json'
        plan = package.read(path)
        command = plan['command']
        package.require(Path(command[command.index('--object-store-dir')+1]) == out/'state/application-objects',
                        'expected shared private object store')
        plan[KEY]['interpreter'] = str(Path(interpreter).absolute())
        package.save(path, plan)


def check(row, previous=None):
    proof = package.read(PROOF)['sdk']
    refs = row['records']
    saved = row['saved']
    package.require(row['sdk'] == proof['sdk'] and package.same_json(refs, proof['records'])
                    and package.same_json(row['arguments'], proof['arguments']) and row['reply'] == proof['reply']
                    and row['finish_reasons'] == ['tool_calls','stop'] and type(row['text_deltas']) is int
                    and row['text_deltas'] > 0, 'incorrect release retrieval or stream terminal')
    package.require(isinstance(saved, dict) and saved['call_id'] == row['call_id']
                    and row['call_id'].startswith('call_') and re.fullmatch('agent_[0-9a-f]{32}', saved['agent_id'])
                    and saved['chat_id'].startswith('chatcmpl-')
                    and re.fullmatch('[0-9a-f]{64}',saved['chat_sha256'])
                    and re.fullmatch('[0-9a-f]{64}',saved['agent_fields_sha256'])
                    and saved['owner_patch'] == 200 and saved['foreign'] == 404,
                    'missing original stored call or scoped update')
    if previous is None:
        package.require(row['restored'] is None, 'unexpected original call')
    else:
        check(previous)
        package.require(row['restored'] == previous['saved']
                        and all(saved[k] != previous['saved'][k] for k in ('call_id','agent_id','chat_id')),
                        'prior evidence or new identities differ')


def exercise(params, interpreter, errors, captures, previous=None):
    request = dict(params, handoff=True, capture=str(captures),
                   release_records={'primary':str(RELEASE), 'previous':str(PREVIOUS)})
    if previous is not None:
        request['previous'] = previous['saved']
    run = subprocess.run([interpreter,'-IB',ACTOR], input=json.dumps(request), text=True,
                         stdout=subprocess.PIPE, stderr=errors, timeout=150, check=True)
    row = json.loads(run.stdout)
    check(row, previous)
    return row
