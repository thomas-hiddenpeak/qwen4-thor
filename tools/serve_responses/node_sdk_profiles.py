"""Recover a device-issued function call from Node across both serving capacities."""
import json
import os
from pathlib import Path
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import approval_identifiers_profiles as approvals
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses import program_sdk_profiles as stored
from tools.serve_responses.node_tool_wire import current_event
from tools.serve_service import package

KEY = 'node_sdk_reader_handoff'
VERSIONS = ('6.34.0', '7.47.0')
ROOT = lifecycle.ROOT / 'docs/evidence/serve-node-sdk-presentation-20261127'
PROOF = ROOT / 'business.json'
RELEASE = ROOT.parent / 'serve-program-sdk-reader-20261127/delivery.json'
ACTOR = Path(__file__).with_name('node_sdk_profile_handoff.mjs')


def writer(phase):
    return {'primary': VERSIONS[0], 'light': VERSIONS[1]}[phase]


def verify_requests(row, origin, continued):
    paths = ['/v1/responses/' + origin[k] for k in ('response_id', 'followup')]
    expected = [('GET', path, 200, False) for path in paths]
    expected.extend(('GET', path, 404, True) for path in paths)
    expected.extend([('GET', paths[0], 200, False)] * 2)
    if continued:
        expected.append(('POST', '/v1/responses', 200, False))
    observed = [(r['method'], r['path'], r['status'], r['peer']) for r in row['requests']]
    package.require(observed == expected and row['foreign_reads'] == [404, 404],
                    'Node SDK did not read the retained objects or issued unexpected requests')


def inspect_pair(directory, row, origin, revision):
    for version in VERSIONS:
        client = row['clients'][version]
        continued = version == writer(row['phase'])
        verify_requests(client, origin, continued)
        named = version == VERSIONS[0]
        package.require(client['named'] is named and client['sdk'] == version,
                        'unexpected Node SDK presentation')
        expected = stored.inspect_capture(directory/version, version, origin, revision,
                                          continued, modern=VERSIONS[1])
        package.require(client['wire'] == expected, 'profile receipt differs from stored files')
    legacy, modern = [directory/v for v in VERSIONS]
    first = package.read(legacy/'events.json')
    index = origin['marker']['output_index']
    call = first[-1]['response']['output'][index]
    for filename in ('events', 'resumed'):
        before = package.read(legacy/(filename + '.json'))
        after = package.read(modern/(filename + '.json'))
        package.require([current_event(e, call, index) for e in before] == after,
                        'Node SDKs received different function journal content')
    for filename in ('call', 'answer'):
        before = package.read(legacy/(filename + '.json'))
        after = package.read(modern/(filename + '.json'))
        package.require(before['prompt_cache_retention'] == 'in-memory' and
                        dict(before, prompt_cache_retention='in_memory') == after,
                        'Node SDKs received different stored responses')


def verify_captures(directory, row):
    inspect_pair(directory, row, package.read(PROOF)['sdk'],
                 package.read(RELEASE)['publication']['publication_revision'])


def check(row, previous=None):
    stored.check(row, previous, versions=VERSIONS)


def exercise(params, directories, errors, captures, previous=None):
    origin = package.read(PROOF)['sdk']
    revision = package.read(RELEASE)['publication']['publication_revision']
    captures.mkdir(mode=0o700)
    row = dict(phase=params['phase'], clients={})
    environment = os.environ.copy()
    if params['base'].startswith('https://'):
        environment['NODE_EXTRA_CA_CERTS'] = str(Path(params['certificate']).resolve(strict=True))
    for version in VERSIONS:
        folder = captures/version
        continued = version == writer(params['phase'])
        options = dict(params, version=version, sdk=directories[version], capture=str(folder),
                       original=origin, revision=revision, continue_tool=continued)
        run = subprocess.run(['node', ACTOR], input=json.dumps(options), text=True,
                             stdout=subprocess.PIPE, stderr=errors, timeout=110, check=True,
                             env=environment)
        data = json.loads(run.stdout)
        data['wire'] = stored.inspect_capture(folder, version, origin, revision, continued,
                                              modern=VERSIONS[1])
        row['clients'][version] = data
    inspect_pair(captures, row, origin, revision)
    check(row, previous)
    return row


def prepare(out, build, deployment, directories, source):
    proof = package.read(PROOF)
    package.require(proof['result']['status'] == 'PASS' and all(
        proof['binaries'][name] == package.sha(build/name) for name in ('q4t', 'q4t-ingress')),
        'foreground acceptance does not identify the capacity candidate')
    package.require(proof['wire_contract']['candidate_bundle'] ==
                    package.sha(lifecycle.ROOT/'docs/serve-openapi-bundle-v1.json'),
                    'candidate streaming declaration changed after business verification')
    witness = ROOT/'source-store.json'
    drivers = [PROOF, RELEASE, witness, ACTOR, Path(__file__), Path(stored.__file__),
               Path(approvals.__file__), Path(__file__).with_name('node_tool_wire.py')]
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.PROGRAM_SDK_REVIEWED,
                                KEY, drivers)
    store = out/'state/application-objects'
    approvals.seed(source, store, package.read(witness))
    clients = {}
    for version in VERSIONS:
        directory = Path(directories[version]).resolve(strict=True)
        package.require(package.read(directory/'package.json')['version'] == version,
                        'wrong Node SDK version on disk')
        clients[version] = str(directory)
    for name in ('primary', 'light'):
        path = out/name/'plan.json'
        plan = package.read(path)
        args = plan['command']
        package.require(Path(args[args.index('--object-store-dir') + 1]) == store,
                        'capacity must reopen the retained Node tool store')
        plan[KEY]['sdk_directories'] = clients
        package.save(path, plan)
