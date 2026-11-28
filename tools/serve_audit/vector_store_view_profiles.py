"""Measure adjacent vector store pages and cleared names on each serving capacity."""
from pathlib import Path
import time
from urllib.parse import urlencode

from tools.serve_recovery import profile_live_reuse as history, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

ROOT = Path(__file__).resolve().parents[2]
KEY = 'vector_store_views'
RECEIPT = 'vector-store-views.json'
CALLS = 18
PROOF = ROOT/'docs/evidence/serve-previous-page-20261128/device.json'
INPUTS = (Path(__file__), PROOF)


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    package.require(proof['status'] == 'PASS' and proof['sdk_reads'] == 72
                    and proof['name_sdk_calls'] == 24 and proof['runtime']['unchanged_default']
                    and proof['candidate_engine'] == package.sha(build/'q4t')
                    and proof['ingress'] == package.sha(build/'q4t-ingress'), 'Untested store-view build')
    lifecycle.prepare_from_manifest(out, build, deployment, proof['installed_manifest'],
                                    history.COMPLETION_LIMIT_REVIEWED, KEY, INPUTS)
    for name in ('primary', 'light'):
        path = out/name/'plan.json'
        plan = package.read(path)
        plan['limits'][KEY] = CALLS
        package.save(path, plan)


def exercise(call, role):
    root = '/v1/vector_stores'
    created = []
    for i in range(5):
        title = 'q4t-' + role + '-' + str(i)
        value = call('POST', root, {'name': title})
        package.require(value['name'] == title and value['object'] == 'vector_store'
                        and value['id'].startswith('vs_') and type(value['created_at']) is int,
                        'Vector store creation differs')
        created.append(value)
    ordered = sorted(created, key=lambda v: (v['created_at'], v['id']))
    ids = [row['id'] for row in ordered]
    titles = {v['id']: v['name'] for v in created}
    package.require(len(titles) == 5, 'Vector store identities collided')
    cleared = ids[2]
    target = root + '/' + cleared
    titles[cleared] = ''
    for value in [call('POST', target, {'name': None}), call('GET', target)]:
        package.require(value['id'] == cleared and value['name'] == '', 'Cleared name differs')
    for order in ('asc', 'desc'):
        values = ids if order == 'asc' else list(reversed(ids))
        cursor = values[-1]
        for end in (4, 2):
            query = urlencode({'order': order, 'limit': 2, 'before': cursor})
            page = call('GET', root + '?' + query)
            expected = values[end-2:end]
            package.require([(v['id'], v['name']) for v in page['data']] ==
                            [(ident, titles[ident]) for ident in expected]
                            and page['first_id'] == expected[0] and page['last_id'] == expected[-1]
                            and page['has_more'] is (end > 2), 'Vector store window or name differs')
            cursor = page['first_id']
    foreign = call('GET', target, client='beta', expected=404)
    package.require('error' in foreign and 'name' not in foreign, 'Foreign store was exposed')
    for ident in ids:
        deleted = call('DELETE', root + '/' + ident)
        package.require(deleted['id'] == ident and deleted['deleted'] is True, 'Vector store deletion differs')
    page = call('GET', root + '?limit=1')
    package.require(page['data'] == [] and page['has_more'] is False, 'Private store cleanup incomplete')


def business(stage):
    rows = []
    def call(method, path, body=None, *, client='alpha', expected=200):
        stage.budget(KEY)
        status, value = stage.api(method, path, client=client, body=body)
        rows.append(dict(method=method, path=path, client=client, body=body, status=status, response=value))
        package.require(status == expected, 'Unexpected vector store HTTP status')
        time.sleep(.125)
        return value
    stage.same_model()
    exercise(call, stage.plan['name'])
    package.require(len(rows) == CALLS, 'Store-view budget differs')
    package.save(stage.out/RECEIPT, rows)
    stage.same_model()


def check(rows, role):
    package.require(len(rows) == CALLS, 'Missing vector store exchanges')
    packets = iter(rows)
    def replay(method, path, body=None, *, client='alpha', expected=200):
        row = next(packets)
        package.require((row['method'], row['path'], row['body'], row['client'], row['status']) ==
                        (method, path, body, client, expected), 'Vector store request or status differs')
        return row['response']
    exercise(replay, role)


def verify(phase, plan, capabilities, current):
    proof = package.read(PROOF)
    change = plan[KEY]
    name = plan['name']
    prior_file = phase/'previous-profile.json'
    prior = profile_record.read(prior_file)
    expected = dict(prior['capabilities'], protocol_source_sha256=change['protocol'])
    package.require(plan['limits'][KEY] == package.read(phase/'result.json')['counts'][KEY] == CALLS
                    and package.sha(prior_file) == change['previous'] == history.COMPLETION_LIMIT_REVIEWED[name]
                    and plan['selected_sha256'] == change['release'] == proof['installed_manifest']
                    and plan['binary_sha256'] == proof['candidate_engine'] and plan['ingress_sha256'] == proof['ingress']
                    and package.same_json(expected, capabilities) and package.same_json(prior['capacity'], plan['capacity'])
                    and all(plan['sources'][str(p.relative_to(ROOT))] == package.sha(p) for p in INPUTS),
                    'Store views differ from the qualified engine, capacity or predecessor')
    receipt = phase/RECEIPT
    check(package.read(receipt), name)
    hashes = prior['business']['live']['evidence_sha256']
    package.require(history.COMPLETION_LIMIT_RECEIPT in hashes, 'Missing predecessor completion evidence')
    return {**{k:v for k,v in hashes.items() if k not in current}, RECEIPT:package.sha(receipt)}
