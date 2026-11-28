"""Attribute the projection and recovery calls to their application SDK."""
import json
from urllib.parse import parse_qs

from .producer_sdk_evidence import capture, require
from .render_sdk_matrix import sha

PROBABILITIES = 'message.output_text.logprobs'
IMAGE = 'message.input_image.image_url'
SDK = 'python-3.55.1'


def check_calls(rows, expected):
    require(len(rows) == len(expected), 'projection request count differs')
    for row, (method, path, status, query) in zip(rows, expected):
        require((row['method'], row['path'], row['status']) == (method, path, status)
                and parse_qs(row['query'], strict_parsing=True, keep_blank_values=True) == query
                and row['trace'], 'projection path, scope, selector or status differs')


def projection_campaigns(directory):
    receipt = directory/'delivery.json'
    delivery = json.loads(receipt.read_bytes())
    publication = delivery['publication']
    digests = delivery['sdk_evidence']
    device = capture(directory/'device.json', digests['device.json'])
    original = capture(directory/'device-sdk.json', digests['device-sdk.json'])
    managed = capture(directory/'managed.json', digests['managed.json'])
    require(publication['status'] == device['status'] == managed['status'] == 'PASS'
            and publication['engine'] == managed['engine'] == device['candidate']
            and delivery['previous_revision'] == device['revision']
            and publication['model_starts'] == 0 and managed['model_starts'] == 4
            and managed['stock_sdk'] == SDK and 'python-'+original['version'] == SDK
            and original['replay_sequence'] == original['original_sequence'][1:]
            and bool(original['replay_sequence']) and original['answer'] == str(device['revision']),
            'projection device, replay or release identity differs')
    common = dict(sdk=SDK, revision=publication['publication_revision'], engine=publication['engine'],
                  registered_during_run=device['revision'], tested_before_registration=True)
    selected = {'include[]': [PROBABILITIES] * 12}
    single = {'include[]': [PROBABILITIES]}
    def view(ident):
        require(isinstance(ident, str) and ident.startswith('resp_') and '/' not in ident,
                'invalid stored response ID')
        return '/v1/responses/'+ident
    response = view(original['response'])
    conversation = '/v1/conversations/'+original['conversation']
    check_calls(original['calls'], [
        ('POST', '/v1/responses', 200, {}), ('GET', response, 200, single),
        ('GET', response, 200, selected),
        ('GET', response, 200, dict(selected, stream=['true'], starting_after=[str(original['original_sequence'][0])])),
        ('GET', response+'/input_items', 200, selected), ('GET', response, 404, selected),
        ('GET', response, 400, {'include[]': selected['include[]']+['invalid']}),
        ('POST', '/v1/conversations', 200, {}),
        ('GET', conversation+'/items', 200, {'include[]': [IMAGE]*12}), ('DELETE', conversation, 200, {})])
    require(len(original['calls']) == device['runtime']['requests'] and device['runtime']['ports_free']
            and device['runtime']['model_exit'] == 0 and device['runtime']['unchanged_default']
            and device['runtime']['starts'] == 1 and device['samples'] > 0, 'projection short run incomplete')
    def source(file, packets, transport, **extra):
        return dict(common, receipt=f'{directory.name}/delivery.json', receipt_sha256=sha(receipt),
                    wire=f'{directory.name}/{file}', wire_sha256=digests[file], transport=transport,
                    requests=len(packets), **extra)
    packets = [(SDK, row) for row in original['calls']]
    yield 'projection-short', packets, device['http_counts'], source('device-sdk.json', packets, 'native TLS')
    ids = {original['response']}
    require(set(managed['roles']) == {'primary', 'fallback'}, 'incomplete recovery scenarios')
    for role, run in managed['roles'].items():
        require(run['model_starts'] == 2 and run['manifest'] == publication[role]
                and run['ports_free'] and run['remaining'] == [] and run['default_unchanged']
                and run['guardian_same'] and run['new_generation'] > run['old_generation']
                and run['nvml_samples'] > 0 and run['core_chat'] == '42', 'recovery incomplete')
        packets = []
        stages = run['requests']
        for mode in ('prepare', 'restore'):
            rows = stages[mode]
            ident = rows['response']
            path = view(ident)
            require(ident not in ids and rows['sdk'] == SDK, 'response ID reused across generations')
            ids.add(ident)
            expected = [('GET', '/v1/capabilities', 200, {})]
            if mode == 'restore':
                old = view(stages['prepare']['response'])
                expected.extend([('GET', old, 200, selected), ('GET', old, 404, selected)])
            expected.extend([('POST', '/v1/responses', 200, {}), ('GET', path, 200, single),
                             ('GET', path, 200, selected), ('GET', path, 404, selected)])
            wire = rows['requests']
            check_calls(wire, expected)
            require(rows['traces'] == [r['trace'] for r in wire]
                    and rows['stock_sdk_indices'] == list(range(1,len(wire))), 'SDK attribution differs')
            packets.extend((SDK, wire[i]) for i in rows['stock_sdk_indices'])
        yield 'projection-'+role, packets, {'200': 7, '404': 3}, source(
            'managed.json', packets, run['transport'], scenario=run['incident'], manifest=run['manifest'])
