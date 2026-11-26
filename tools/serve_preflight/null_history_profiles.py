"""Qualify original nullable Chat history across the registered capacities."""
import json
from pathlib import Path
import re
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

ROOT = lifecycle.ROOT
KEY = 'null_assistant_history_handoff'
EVIDENCE = ROOT/'docs/evidence'
PROOF = EVIDENCE/'serve-null-assistant-history-20261126/business.json'
RELEASE = EVIDENCE/'serve-reclaim-pressure-20261126/delivery.json'
LOOKUP = EVIDENCE/'serve-tool-map-references-20261126/delivery.json'
ACTOR = Path(__file__).with_name('null_history_sdk.py')


def prepare(out, build, deployment, interpreter):
    proof = package.read(PROOF)
    version = subprocess.check_output([interpreter, '-IB', '-c',
        'import openai;print(openai.__version__)'], text=True, timeout=15).strip()
    package.require(proof['stage'] == 'verified_native_https' and proof['result']['status'] == 'PASS'
                    and version == proof['client']['sdk']
                    and proof['sdk_driver_sha256'] == package.sha(ACTOR)
                    and proof['engine_sha256'] == package.sha(build/'q4t')
                    and proof['ingress_sha256'] == package.sha(build/'q4t-ingress'),
                    'nullable history needs an accepted HTTPS build and SDK')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.TOOL_MAP_REVIEWED,
                                KEY, [PROOF, RELEASE, LOOKUP, ACTOR, Path(__file__)])
    for name in ('primary', 'light'):
        path = out/name/'plan.json'
        plan = package.read(path)
        command = plan['command']
        package.require(Path(command[command.index('--object-store-dir')+1]) ==
                        out/'state/application-objects', 'shared private history required')
        plan[KEY]['interpreter'] = str(Path(interpreter).absolute())
        package.save(path, plan)


def check(row, previous=None):
    accepted = package.read(PROOF)['client']
    for key in ('sdk', 'revision', 'source_sha256', 'reply', 'finishes',
                'independent_chat', 'missing_content_status'):
        package.require(package.same_json(row[key], accepted[key]), 'history result differs: '+key)
    package.require(type(row['usage']) is int and row['usage'] > 0 and
                    type(row['chunks']) is int and row['chunks'] >= 2, 'missing stream completion')
    saved = row['saved']
    package.require(saved['peer'] == 404 and saved['strict_empty_page'] is True
                    and saved['strict_mixed_page'] is accepted['saved']['strict_mixed_page']
                    and re.fullmatch(r'chatcmpl-[A-Za-z0-9_-]+',saved['chat_id'])
                    and re.fullmatch(r'call_[A-Za-z0-9_-]+',saved['call_id'])
                    and re.fullmatch(r'chatmsg_[0-9a-f]{64}',saved['empty_id'])
                    and re.fullmatch(r'[0-9a-f]{64}',saved['history_sha256']),
                    'missing retained history identity or owner isolation')
    if previous is None:
        package.require(row['restored'] is None, 'unexpected previous history')
    else:
        check(previous)
        package.require(package.same_json(row['restored'], previous['saved'])
                        and all(saved[k] != previous['saved'][k] for k in ('empty_id','chat_id','call_id')),
                        'original history changed or new identifiers were reused')


def exercise(params, interpreter, errors, captures, previous=None):
    query = package.read(LOOKUP)['publication']
    request = dict(params, capture=str(captures), handoff=True, release_record=str(LOOKUP),
                   revision=query['publication_revision'])
    if previous is not None:
        request['previous'] = previous['saved']
    run = subprocess.run([interpreter,'-IB',ACTOR], input=json.dumps(request),
                         text=True, stdout=subprocess.PIPE, stderr=errors, timeout=150, check=True)
    row = json.loads(run.stdout)
    check(row, previous)
    return row
