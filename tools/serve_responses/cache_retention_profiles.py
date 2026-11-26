"""Verify retained Responses across capacity profiles and strict SDK generations."""
import hashlib
import json
from pathlib import Path
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import approval_identifiers_profiles, environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

KEY = 'cache_retention_handoff'
ROOT = lifecycle.ROOT / 'docs/evidence/serve-response-cache-retention-20261127'
PROOF = ROOT / 'business.json'
RELEASE = ROOT.parent / 'serve-video-layer-composition-20261127/delivery.json'
ACTOR = lifecycle.ROOT / 'tools/serve_audit/response_cache_retention.py'
LEGACY = '2.8.1'
CURRENT = '3.55.1'


def stable_hash(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode()).hexdigest()


def read_capture(directory, reply):
    saved = package.read(directory/'resource.json')
    events = package.read(directory/'replay.json')
    spelling = 'in-memory' if reply['sdk'] == LEGACY else 'in_memory'
    package.require(reply['sdk'] in (LEGACY, CURRENT) and reply['foreign'] == 404
                    and reply['get_sha256'] == package.sha(directory/'resource.json')
                    and saved['id'] == reply['id'] and saved['status'] == 'completed'
                    and saved['metadata']['prompt_cache_retention'] == 'in_memory'
                    and saved['prompt_cache_options'] == reply['cache_options']
                    and reply['retention'] == saved['prompt_cache_retention'] == spelling,
                    'SDK, resource identity, metadata or negotiated retention changed')
    package.require(len(events) == reply['events'] and events[0]['type'] == 'response.created'
                    and events[-1]['type'] == 'response.completed'
                    and events[-1]['response'] == saved
                    and all(e['sequence_number'] == i for i, e in enumerate(events)),
                    'incomplete or inconsistent Responses replay')
    for event in events:
        if 'response' in event:
            value = event['response']
            package.require(value['id'] == saved['id'] and value['prompt_cache_retention'] == spelling,
                            'wrong resource or retention in a saved event')
            value['prompt_cache_retention'] = 'in_memory'
    # The SDKs serialize this typed timestamp as an int or a float. Compare it
    # to the original GET number before hashing the completed event.
    terminal = events[-1]['response']
    if 'completed_at' in saved and saved['completed_at'] is not None:
        timestamp = saved['completed_at']
        package.require(type(timestamp) in (int, float)
                        and type(terminal['completed_at']) in (int, float)
                        and terminal['completed_at'] == timestamp,
                        'typed completion time differs from the HTTP resource')
        terminal['completed_at'] = timestamp
    saved['prompt_cache_retention'] = 'in_memory'
    return dict(id=saved['id'], resource=stable_hash(saved), events=stable_hash(events))


def verify_captures(directory, row):
    original = package.read(PROOF)['revision']
    observations = {**row['history'], 'created': row['created']}
    if 'restored' in row:
        observations['restored'] = row['restored']
    for name, value in observations.items():
        folder = directory/name
        package.require(read_capture(folder, value['client']) == value['canonical'],
                        'the captured body or event sequence differs from its receipt')
        reply = package.read(folder/'resource.json')
        text = ''.join(p['text'] for item in reply['output'] if item['type'] == 'message'
                       for p in item['content'] if p['type'] == 'output_text')
        expected = original if name in row['history'] else row['revision']
        package.require(text.strip() == str(expected), 'release query returned an incorrect revision')
        if name == 'created':
            package.require(package.read(folder/'create.json') == package.read(folder/'replay.json'),
                            'fresh stream and retained replay differ')


def prepare(out, build, deployment, interpreters, source):
    business = package.read(PROOF)
    package.require(business['result']['status'] == 'PASS'
                    and all(business['compiled'][n] == package.sha(build/n) for n in ('q4t','q4t-ingress')),
                    'profile check requires the candidate verified by native TLS')
    for version in (LEGACY, CURRENT):
        actual = subprocess.check_output([interpreters[version], '-IB', '-c',
                                          'import openai; print(openai.__version__)'], text=True, timeout=10)
        package.require(actual.strip() == version, 'unexpected installed Python SDK')
    witness = package.read(ROOT/'source-store.json')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.LAYER_REVIEWED, KEY,
                                [ACTOR, PROOF, ROOT/'source-store.json', Path(__file__),
                                 Path(approval_identifiers_profiles.__file__)])
    store = out/'state/application-objects'
    approval_identifiers_profiles.seed(source, store, witness)
    for name in ('primary','light'):
        path = out/name/'plan.json'
        plan = package.read(path)
        args = plan['command']
        package.require(Path(args[args.index('--object-store-dir') + 1]) == store,
                        'selected profiles must share the restored object store')
        plan[KEY]['interpreters'] = {v: str(Path(interpreters[v]).absolute()) for v in (LEGACY,CURRENT)}
        package.save(path, plan)


def check(row, previous=None):
    proof = package.read(PROOF)
    expected = package.read(RELEASE)['publication']['publication_revision']
    phase = 'primary' if previous is None else 'light'
    writer = LEGACY if phase == 'primary' else CURRENT
    reader = CURRENT if writer == LEGACY else LEGACY
    package.require(row['phase'] == phase and row['revision'] == expected
                    and set(row['history']) == {'old','current'}, 'missing profile or prior history')
    for name, history in row['history'].items():
        accepted = proof['receipts'][name]
        ref = next(r for r in (accepted['create'], accepted['read']) if r['sdk'] == reader)
        package.require(history['client']['id'] == ref['id'] and history['client']['sdk'] == reader
                        and history['client']['get_sha256'] == ref['get_sha256']
                        and history['canonical']['id'] == ref['id'], 'saved response representation changed')
    made = row['created']
    package.require(made['client']['sdk'] == writer and made['client']['foreign'] == 404
                    and made['canonical']['id'] == made['client']['id']
                    and made['client']['emitted'] == made['client']['events'] > 0
                    and made['client']['id'] not in {v['canonical']['id'] for v in row['history'].values()},
                    'missing fresh streamed response to the release query')
    if previous is None:
        package.require('restored' not in row, 'unexpected prior profile')
        return
    check(previous)
    restored = row['restored']
    package.require(restored['client']['sdk'] == CURRENT and restored['canonical'] == previous['created']['canonical']
                    and restored['client']['get_sha256'] != previous['created']['client']['get_sha256']
                    and made['client']['id'] != restored['client']['id']
                    and all(v['canonical'] == previous['history'][n]['canonical'] for n,v in row['history'].items()),
                    'recovery changed the stored response or event history')


def exercise(params, interpreters, errors, captures, previous=None):
    captures.mkdir(mode=0o700)
    proof = package.read(PROOF)
    revision = package.read(RELEASE)['publication']['publication_revision']
    def invoke(label, version, **options):
        directory = captures/label
        request = dict(params, capture=str(directory), check_metadata=True,
                       expected='in-memory' if version == LEGACY else 'in_memory', **options)
        run = subprocess.run([interpreters[version], '-IB', ACTOR], input=json.dumps(request), text=True,
                             stdout=subprocess.PIPE, stderr=errors, check=True, timeout=85)
        reply = json.loads(run.stdout)
        package.require(reply['sdk'] == version, 'wrong SDK for the handoff')
        return dict(client=reply, canonical=read_capture(directory, reply))

    phase = params['phase']
    writer, reader = (LEGACY, CURRENT) if phase == 'primary' else (CURRENT, LEGACY)
    row = dict(phase=phase, revision=revision, history={})
    for name, replies in proof['receipts'].items():
        if name in ('old','current'):
            row['history'][name] = invoke(name, reader, operation='read', id=replies['create']['id'],
                                          text=str(proof['revision']))
    if previous is not None:
        row['restored'] = invoke('restored', CURRENT, operation='read',
                                 id=previous['created']['canonical']['id'], text=str(revision))
    prompt = 'Project release record: ' + json.dumps({'publication_revision': revision})
    row['created'] = invoke('created', writer, operation='create', stream=True,
                            requested='in-memory' if writer == LEGACY else 'in_memory',
                            prompt=prompt+'. Return only the integer publication_revision.', text=str(revision))
    verify_captures(captures, row)
    check(row, previous)
    return row
