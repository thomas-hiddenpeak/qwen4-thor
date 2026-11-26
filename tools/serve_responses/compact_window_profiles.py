"""Replay authenticated Responses windows from a closed store after a new start."""
import hashlib
import json
from pathlib import Path
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import approval_identifiers_profiles as approvals
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

KEY = 'compact_window_handoff'
ROOT = lifecycle.ROOT / 'docs/evidence/serve-compact-window-identities-20261127'
PROOF = ROOT / 'business.json'
RELEASE = ROOT.parent / 'serve-mcp-error-sdk-20261127/delivery.json'
ACTOR = lifecycle.ROOT / 'tools/serve_responses/compact_window_sdk.py'
CASES = ('release-old', 'release-current', 'video')


def read_window(path, digest):
    data = Path(path).read_bytes()
    package.require(hashlib.sha256(data).hexdigest() == digest,
                    'saved compaction window differs from the device receipt')
    return json.loads(data)


def check(row, previous=None):
    expected = package.read(PROOF)['receipts']
    package.require(row['phase'] == ('primary' if previous is None else 'light')
                    and set(row['cases']) == set(CASES), 'unexpected profile or replay set')
    for name in CASES:
        item = row['cases'][name]
        ref = expected[name]
        package.require(all(item[k] == ref[k] for k in
                            ('window_sha256', 'sdk', 'answer', 'foreign_code', 'http_status'))
                        and item['exact_answer'] is True and item['usage']['output_tokens'] > 0
                        and item['response_id'].startswith('resp_'),
                        'saved window failed owner, SDK or foreign-client replay')
    ids = {row['cases'][name]['response_id'] for name in CASES}
    package.require(len(ids) == len(CASES), 'separate continuations shared a response')
    if previous is not None:
        package.require(previous['phase'] == 'primary'
                        and ids.isdisjoint(x['response_id'] for x in previous['cases'].values()),
                        'profile continuation reused an earlier response')


def verify_captures(directory, row):
    for name in CASES:
        item = row['cases'][name]
        path = directory / name / 'response.json'
        response = package.read(path)
        output = response['output']
        answer = ''.join(content['text'] for message in output if message['type'] == 'message'
                         for content in message['content'] if content['type'] == 'output_text')
        package.require(package.sha(path) == item['response_sha256']
                        and response['id'] == item['response_id']
                        and response['status'] == 'completed' and response['error'] is None
                        and all(part['type'] != 'mcp_call' for part in output)
                        and response['usage']['output_tokens'] == item['usage']['output_tokens']
                        and answer.strip() == item['answer'],
                        'profile receipt differs from the completed response')


def exercise(params, settings, errors, captures, previous=None):
    references = package.read(PROOF)['receipts']
    row = dict(phase=params['phase'], cases={})
    captures.mkdir(mode=0o700)
    for name in CASES:
        ref = references[name]
        window = read_window(settings['windows'][name], ref['window_sha256'])
        capture = captures / name
        options = dict(params, operation='replay', window=window, answer=ref['answer'],
                       capture=str(capture))
        # Isolated Python excludes the checkout from sys.path. Add only
        # the reviewed project root so the SDK actor can import CompactWindow.
        cmd = [settings['interpreters'][ref['sdk']], '-IB', '-c',
               'import runpy,sys;sys.path.insert(0,sys.argv[1]);'
               'runpy.run_module("tools.serve_responses.compact_window_sdk",run_name="__main__")',
               str(lifecycle.ROOT)]
        result = subprocess.run(cmd, input=json.dumps(options), text=True, check=True,
                                stdout=subprocess.PIPE, stderr=errors, timeout=120)
        receipt = json.loads(result.stdout)
        row['cases'][name] = dict(receipt, window_sha256=ref['window_sha256'],
                                  response_id=package.read(capture / 'response.json')['id'])
    verify_captures(captures, row)
    check(row, previous)
    return row


def prepare(out, build, deployment, interpreters, source, windows):
    proof = package.read(PROOF)
    package.require(proof['result']['status'] == 'PASS' and
                    all(proof['compiled']['files'][name] == package.sha(build / name)
                        for name in ('q4t', 'q4t-ingress')), 'compaction business binary differs')
    package.require(set(windows) == set(CASES), 'missing signed compact windows')
    for name in CASES:
        read_window(windows[name], proof['receipts'][name]['window_sha256'])
    drivers = [ACTOR, ACTOR.with_name('compact_client.py'), PROOF, ROOT / 'source-store.json',
               Path(__file__), Path(approvals.__file__)]
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.MCP_ERROR_REVIEWED,
                                KEY, drivers)
    store = out / 'state/application-objects'
    approvals.seed(source, store, package.read(ROOT / 'source-store.json'))
    for phase in ('primary', 'light'):
        path = out / phase / 'plan.json'
        plan = package.read(path)
        cmd = plan['command']
        package.require(Path(cmd[cmd.index('--object-store-dir') + 1]) == store,
                        'profile must use the seeded object store')
        plan[KEY].update(interpreters={v: str(Path(p).absolute()) for v,p in interpreters.items()},
                         windows={case: str(Path(p).absolute()) for case,p in windows.items()})
        package.save(path, plan)
