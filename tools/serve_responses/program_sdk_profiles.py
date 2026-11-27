"""Qualify the old and current SDK views of an actual pending tool response."""
import json
from pathlib import Path
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import approval_identifiers_profiles as approvals
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

KEY = 'program_sdk_reader_handoff'
VERSIONS = ('2.8.1', '3.55.1')
ROOT = lifecycle.ROOT / 'docs/evidence/serve-program-sdk-reader-20261127'
PROOF = ROOT / 'business.json'
RELEASE = ROOT.parent / 'serve-compact-inline-media-20261127/delivery.json'
ACTOR = Path(__file__).with_name('program_sdk_handoff.py')


def writing_sdk(phase):
    return {'primary': VERSIONS[0], 'light': VERSIONS[1]}[phase]


def answer_text(body):
    messages = [item for item in body['output'] if item['type'] == 'message']
    package.require(body['status'] == 'completed' and body['error'] is None
                    and body['incomplete_details'] is None and messages
                    and all(item['status'] == 'completed' for item in messages),
                    'tool response did not complete')
    return ''.join(part['text'] for item in messages
                   for part in item['content'] if part['type'] == 'output_text').strip()


def inspect_capture(folder, version, origin, revision, continued):
    body = package.read(folder/'call.json')
    answer = package.read(folder/'answer.json')
    events = package.read(folder/'events.json')
    resumed = package.read(folder/'resumed.json')
    calls = [item for item in body['output'] if item['type'] == 'function_call']
    package.require(len(calls) == 1 and body['id'] == origin['response_id']
                    and body['status'] == 'completed' and body['error'] is None
                    and body['incomplete_details'] is None
                    and calls[0]['id'] == origin['item_id']
                    and calls[0]['call_id'] == origin['call_id']
                    and calls[0]['name'] == origin['function']
                    and json.loads(calls[0]['arguments']) == {},
                    'wrong original tool identity or arguments')
    index = body['output'].index(calls[0])
    marker = dict(origin['marker'])
    package.require(marker['item_id'] == calls[0]['id'] and marker['name'] == calls[0]['name']
                    and marker['arguments'] == calls[0]['arguments']
                    and marker['sequence_number'] == origin['cursor'] + 1,
                    'captured argument marker belongs to a different function or cursor')
    if version == VERSIONS[1]:
        marker.pop('name')
    journal = [event for event in events if event['type'] == 'response.function_call_arguments.done']
    require = package.require
    require(len(events) == origin['frames']
            and [e['sequence_number'] for e in events] == list(range(len(events)))
            and journal == [marker] and marker['output_index'] == index
            and resumed == events[origin['cursor'] + 1:] and resumed and resumed[0] == marker
            and [e['type'] for e in events].count('response.completed') == 1
            and events[-1]['type'] == 'response.completed' and events[-1]['response'] == body,
            'strict function stream or resumed suffix differs from the stored response')
    require(answer['id'] == origin['followup'] and answer['previous_response_id'] == body['id']
            and answer_text(answer) == str(origin['revision']), 'stored tool answer changed')
    paths = ['call', 'answer', 'events', 'resumed']
    fresh_id = None
    if continued:
        fresh = package.read(folder/'continuation.json')
        fresh_id = fresh['id']
        require(fresh_id.startswith('resp_') and fresh_id not in (body['id'], answer['id'])
                and fresh['previous_response_id'] == body['id']
                and answer_text(fresh) == str(revision), 'invalid new tool continuation')
        paths.append('continuation')
    return dict(hashes={key: package.sha(folder/(key+'.json')) for key in paths},
                continued=fresh_id)


def verify_captures(directory, row):
    original = package.read(PROOF)['sdk']
    revision = package.read(RELEASE)['publication']['publication_revision']
    for version in VERSIONS:
        actual = inspect_capture(directory/version, version, original, revision,
                                 version == writing_sdk(row['phase']))
        package.require(actual == row['clients'][version]['wire'], 'SDK receipts differ from disk')


def check(row, previous=None):
    package.require(row['phase'] == ('primary' if previous is None else 'light')
                    and set(row['clients']) == set(VERSIONS), 'unexpected SDK set or capacity')
    for version in VERSIONS:
        data = row['clients'][version]
        package.require(data['sdk'] == version and data['foreign_reads'] == [404, 404],
                        'incorrect SDK or scope for the retained tool call')
        if previous is not None:
            old = previous['clients'][version]['wire']['hashes']
            now = data['wire']['hashes']
            package.require(all(old[key] == now[key] for key in ('call','answer','events','resumed')),
                            'stored response bytes changed after profile replacement')
    if previous is not None:
        primary_id = previous['clients'][VERSIONS[0]]['wire']['continued']
        light_id = row['clients'][VERSIONS[1]]['wire']['continued']
        package.require(primary_id and light_id and primary_id != light_id,
                        'two profile continuations reused an identity')


def exercise(params, interpreters, errors, captures, previous=None):
    origin = package.read(PROOF)['sdk']
    revision = package.read(RELEASE)['publication']['publication_revision']
    row = dict(phase=params['phase'], clients={})
    captures.mkdir(mode=0o700)
    for version in VERSIONS:
        folder = captures/version
        continued = version == writing_sdk(params['phase'])
        options = dict(params, original=origin, revision=revision, capture=str(folder),
                       continue_tool=continued)
        result = subprocess.run([interpreters[version], '-IB', ACTOR], input=json.dumps(options),
                                text=True, stdout=subprocess.PIPE, stderr=errors,
                                timeout=110, check=True)
        data = json.loads(result.stdout)
        data['wire'] = inspect_capture(folder, version, origin, revision, continued)
        row['clients'][version] = data
    check(row, previous)
    return row


def prepare(out, build, deployment, interpreters, source):
    proof = package.read(PROOF)
    package.require(proof['result']['status'] == 'PASS' and all(
                    proof['build']['files'][name] == package.sha(build/name)
                    for name in ('q4t', 'q4t-ingress')), 'foreground business or binary changed')
    witness = ROOT/'source-store.json'
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.COMPACT_MEDIA_REVIEWED,
                                KEY, [PROOF, RELEASE, witness, ACTOR, Path(__file__),
                                      Path(approvals.__file__)])
    store = out/'state/application-objects'
    approvals.seed(source, store, package.read(witness))
    for label in ('primary', 'light'):
        path = out/label/'plan.json'
        plan = package.read(path)
        args = plan['command']
        package.require(Path(args[args.index('--object-store-dir') + 1]) == store,
                        'profiles must reopen the same closed store')
        plan[KEY]['interpreters'] = {v: str(Path(interpreters[v]).absolute()) for v in VERSIONS}
        package.save(path, plan)
