"""Check failed Responses across capacity changes and owned process replacement."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

KEY = 'response_failure_handoff'
RECEIPT = 'response-failure-handoff.json'
ACTOR = Path(__file__).with_name('failed_response_sdk.py')
PROOF = lifecycle.ROOT / 'docs/evidence/serve-responses-errors-20261126/business.json'
RELEASE = lifecycle.ROOT / 'docs/evidence/serve-sdk-routes-20261126/delivery.json'
DIGEST = re.compile(r'[0-9a-f]{64}')


def seed(source, store):
    witness = package.read(PROOF)['strict_python']
    source = Path(source)
    filename = r'[0-9a-f]{64}\.response\.' + re.escape(witness['id']) + r'\.json'
    package.require(re.fullmatch(filename, source.name) and source.is_file() and not source.is_symlink() and
                    package.sha(source) == witness['source_sha256'], 'interrupted response source changed')
    data = package.read(source)['record']['data']
    response = data['response']
    last = data['events'][-1]
    package.require(response['id'] == witness['id'] and response['status'] == 'failed' and
                    response['error']['code'] == witness['cause'] and last['type'] == 'response.failed'
                    and last['sequence_number'] == witness['sequence'], 'wrong historical failure or cursor')
    store.mkdir(mode=0o700, exist_ok=True, parents=True)
    with source.open('rb') as original, os.fdopen(os.open(
            store/source.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as dest:
        os.fchmod(dest.fileno(), 0o600)
        shutil.copyfileobj(original, dest)
    package.require(package.sha(store/source.name) == witness['source_sha256'], 'incomplete fixture copy')
    return dict(id=response['id'], cause=response['error']['code'], message=response['error']['message'],
                last_sequence=last['sequence_number'])


def prepare(out, build, deployment, interpreter, source):
    proof = package.read(PROOF)
    interpreter = Path(interpreter).absolute()
    version = subprocess.check_output([interpreter, '-IB', '-c', 'import openai; print(openai.__version__)'],
                                      timeout=15, text=True).strip()
    package.require(version == proof['strict_python']['sdk'] == '3.55.1' and
                    proof['status'] == 'native_tls_business_pass' and proof['production_defaults_unchanged']
                    and proof['engine'] == package.sha(build/'q4t')
                    and proof['ingress'] == package.sha(build/'q4t-ingress'), 'unverified failure candidate or SDK')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.EVAL_CANCEL_REVIEWED,
                                KEY, [ACTOR, PROOF, RELEASE, Path(__file__)])
    store = out/'state/application-objects'
    historic = seed(source, store)
    for name in ('primary', 'light'):
        path = out/name/'plan.json'
        plan = package.read(path)
        command = plan['command']
        package.require(Path(command[command.index('--object-store-dir') + 1]) == store,
                        'historical replay must use the private handoff store')
        plan[KEY].update(interpreter=str(interpreter), fixture=historic)
        package.save(path, plan)


def check(row, previous=None):
    witness = package.read(PROOF)['strict_python']
    fields = ('sdk','strict','id','get','cancel','resumed','public_code','cause','peer','sequence')
    wire = row['captures']
    historical = ('get.json', 'cancel.json', 'decoded-event.json')
    active = row['live']
    package.require(all(row[k] == witness[k] for k in fields) and
                    all(wire[k] == witness['captures'][k] for k in historical),
                    'historical response, terminal event or access control changed')
    package.require(re.fullmatch(r'resp_[0-9a-f]{32}', active['id']) and active['id'] != row['id']
                    and active['code'] == 'server_error' and active['cause'] == 'explicit_cancel'
                    and active['control'] == 202 and active['get'] == 200
                    and type(active['sequence']) is int and 1 <= active['sequence'] < 1200
                    and all(DIGEST.fullmatch(wire[name]) for name in
                            ('live-get.json','live-decoded-terminal.json','live-sequence.jsonl')),
                    'invalid fresh failure, stream cursor or capture')
    if previous is None:
        package.require(row['restored'] is None, 'unexpected earlier response')
        return
    check(previous)
    prior = previous['live']
    restored = dict(id=prior['id'], sequence=prior['sequence'], get=200, peer=404,
                    resource_sha256=previous['captures']['live-get.json'],
                    terminal_sha256=previous['captures']['live-decoded-terminal.json'])
    package.require(active['id'] != prior['id'] and row['restored'] == restored and
                    wire['restored-get.json'] == restored['resource_sha256'] and
                    wire['restored-terminal.json'] == restored['terminal_sha256'],
                    'prior response or resumed terminal changed across handoff')


def exercise(params, interpreter, errors, captures, previous=None):
    args = dict(params, capture=str(captures), max_output_tokens=96)
    if previous is not None:
        args['previous'] = previous['live']
    proc = subprocess.run([interpreter, '-IB', ACTOR], input=json.dumps(args), text=True,
                          stdout=subprocess.PIPE, stderr=errors, timeout=75, check=True)
    row = json.loads(proc.stdout)
    check(row, previous)
    package.require(row['captures'] == {p.name: package.sha(p) for p in captures.iterdir()},
                    'failure SDK capture changed')
    return row
