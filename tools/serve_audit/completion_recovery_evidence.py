"""Attribute recovered text completions to the client and package that served them."""
import json

from .producer_sdk_evidence import capture, require
from .render_sdk_matrix import sha

SDK = 'python-3.55.1'


def recovered_calls(managed, publication, previous_revision):
    roles = managed['roles']
    require(managed['status'] == 'PASS' and managed['stock_sdk'] == SDK
            and managed['engine'] == publication['engine'] and managed['model_starts'] == 4
            and set(roles) == {'primary', 'fallback'}, 'Incomplete completion recovery')
    require(roles['primary']['registered_fallback'] == publication['fallback']
            and publication['fallback'] != publication['primary'], 'Fallback registration differs')
    seen, completions = set(), set()
    for role, run in roles.items():
        require(run['incident'] == ('model_exit' if role == 'fallback' else 'primary_gateway_SIGABRT')
                and run['manifest'] == publication[role] and run['model_starts'] == 2
                and run['new_generation'] > run['old_generation'] and run['guardian_same']
                and run['core_chat'] == '42' and run['nvml_samples'] > 0
                and run['ports_free'] and run['default_unchanged'] and not run['remaining'],
                'Incomplete model recovery or gateway takeover')
        for phase in ('prepare', 'restore'):
            capture = run['requests'][phase]
            wires = capture['requests']
            require(len(wires) == 5 and capture['stock_sdk_indices'] == [1, 2, 3, 4]
                    and capture['sdk'] == SDK and str(previous_revision) == capture['answer']
                    and capture['completion'].startswith('cmpl-') and capture['completion'] not in completions,
                    'Completion SDK evidence, generated answer or result identity differs')
            completions.add(capture['completion'])
            traces = [r['trace'] for r in wires]
            require(traces == capture['traces'] and all(isinstance(t, str) and t for t in traces)
                    and len(set(traces)) == 5 and not seen.intersection(traces), 'Repeated or missing request ID')
            seen.update(traces)
            require([(r['method'], r['path'], r['status'], r['query']) for r in wires] ==
                    [('GET', '/v1/capabilities', 200, '')] +
                    [('POST', '/v1/completions', s, '') for s in (400, 200, 200, 200)],
                    'Recovered completion operation or result status differs')
            first, second, independent, seeded = [r['body'] for r in wires[1:]]
            prompt = 'q4t managed completion ' + phase
            echo = dict(model='qwen3.8-flash-next', prompt=prompt, echo=True, max_tokens=0)
            require(first == dict(echo, best_of=21) and second == dict(echo, best_of=20)
                    and independent == dict(echo, n=21) and seeded['seed'] == 17
                    and seeded['temperature'] == 0 and seeded['logprobs'] == 1
                    and f'publication_revision: {previous_revision}\n' in seeded['prompt'],
                    'Completion boundary, independent result count or seeded release record differs')
            info = dict(sdk=SDK, transport=run['transport'], engine=managed['engine'],
                        manifest=publication['fallback'] if phase == 'restore' else publication[role],
                        generation=run['new_generation'] if phase == 'restore' else run['old_generation'],
                        incident=run['incident'], requests=4, direct_http_controls=1)
            yield 'completion-' + role + '-' + phase, [(SDK, row) for row in wires[1:]], info


def completion_recovery_campaigns(directory):
    receipt = directory / 'delivery.json'
    delivery = json.loads(receipt.read_bytes())
    publication = delivery['publication']
    managed_path = directory / 'managed.json'
    digest = delivery['sdk_evidence']['managed.json']
    managed = capture(managed_path, digest)
    prior = json.loads((directory / delivery['previous_delivery']).read_bytes())['publication']
    require(publication['status'] == prior['status'] == 'PASS' and publication['model_starts'] == 0
            and managed['package_source'] == delivery['signed_source']['head']
            and delivery['short_receipt_sha256'] == digest
            and prior['publication_revision'] == delivery['previous_revision'],
            'Completion release lineage or managed evidence differs')
    for name, packets, info in recovered_calls(managed, publication, delivery['previous_revision']):
        yield name, packets, {'200': 3, '400': 1}, dict(info,
            revision=publication['publication_revision'], receipt=f'{directory.name}/delivery.json',
            receipt_sha256=sha(receipt), wire=f'{directory.name}/managed.json', wire_sha256=digest)
