"""Verify strict SDK representations when a new process reopens one customer's MCP journal."""
import json
from pathlib import Path
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import approval_identifiers_profiles as approvals
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

KEY = 'mcp_error_sdk_handoff'
ROOT = lifecycle.ROOT / 'docs/evidence/serve-mcp-error-sdk-20261127'
PROOF = ROOT / 'business.json'
RELEASE = ROOT.parent / 'serve-response-cache-retention-20261127/delivery.json'
ACTOR = lifecycle.ROOT / 'tools/serve_audit/mcp_error_sdk.py'
VERSIONS = ('2.8.1', '3.55.1')


def wire_hashes(directory):
    return {name: package.sha(directory / name) for name in ('resource.json', 'replay.json')}


def validate_read(receipt, reference, version):
    require = package.require
    fields = ('id', 'approved_id', 'sdk', 'get_sha256', 'events', 'cursor', 'resumed')
    require(all(receipt[k] == reference[k] for k in fields)
            and receipt['sdk'] == version
            and receipt['string_error'] is (version == VERSIONS[0])
            and receipt['foreign_404'] == 2
            and receipt['events'] - receipt['cursor'] - 1 == receipt['resumed'] > 0,
            'MCP response, cursor, reader or authorization changed')


def check(row, previous=None):
    proof = package.read(PROOF)
    require = package.require
    require(row['phase'] == ('primary' if previous is None else 'light')
            and set(row['clients']) == set(VERSIONS), 'unexpected MCP profile or reader set')
    for version in VERSIONS:
        item = row['clients'][version]
        validate_read(item['receipt'], proof['clients'][version], version)
        require(item['wire_sha256'] == proof['wire_sha256'][version],
                'issued GET bytes or ordered events changed for a strict reader')
    if previous is not None:
        require(previous['phase'] == 'primary' and package.same_json(row['clients'], previous['clients']),
                'profile change altered a retained MCP decision')


def verify_captures(directory, row):
    for version, client in row['clients'].items():
        folder = directory / version
        body = package.read(folder / 'resource.json')
        events = package.read(folder / 'replay.json')
        receipt = client['receipt']
        package.require(client['wire_sha256'] == wire_hashes(folder)
                        and receipt['get_sha256'] == client['wire_sha256']['resource.json']
                        and body['id'] == receipt['id']
                        and len(events) == receipt['events']
                        and events[-1]['response']['output'] == body['output'],
                        'on-disk MCP capture differs from the received resource or journal')


def exercise(params, interpreters, errors, captures, previous=None):
    proof = package.read(PROOF)
    captures.mkdir(mode=0o700)
    row = dict(phase=params['phase'], clients={})
    for version in VERSIONS:
        ref = proof['clients'][version]
        folder = captures / version
        options = dict(params, capture=str(folder), operation='read', id=ref['id'],
                       approved_id=ref['approved_id'], string_error=version == VERSIONS[0])
        result = subprocess.run([interpreters[version], '-IB', ACTOR], input=json.dumps(options),
                                text=True, stdout=subprocess.PIPE, stderr=errors,
                                timeout=60, check=True)
        row['clients'][version] = dict(receipt=json.loads(result.stdout), wire_sha256=wire_hashes(folder))
    verify_captures(captures, row)
    check(row, previous)
    return row


def prepare(out, build, deployment, interpreters, source):
    proof = package.read(PROOF)
    package.require(proof['result']['status'] == 'PASS' and
                    all(proof['compiled'][name] == package.sha(build / name)
                        for name in ('q4t', 'q4t-ingress')), 'MCP business binary differs')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.CACHE_RETENTION_REVIEWED,
                                KEY, [ACTOR, PROOF, ROOT / 'source-store.json',
                                      Path(__file__), Path(approvals.__file__)])
    store = out / 'state/application-objects'
    approvals.seed(source, store, package.read(ROOT / 'source-store.json'))
    for label in ('primary', 'light'):
        path = out / label / 'plan.json'
        plan = package.read(path)
        args = plan['command']
        package.require(Path(args[args.index('--object-store-dir') + 1]) == store,
                        'MCP profiles must reopen the same captured store')
        plan[KEY]['interpreters'] = {v: str(Path(interpreters[v]).absolute()) for v in VERSIONS}
        package.save(path, plan)
