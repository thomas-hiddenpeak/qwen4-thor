"""Attribute model lookup, text generation and vector results to issued SDK calls."""
from collections import Counter
import json

from .model_inference_receipt import verify
from .producer_sdk_evidence import capture, require
from .render_sdk_matrix import sha


def model_inference_campaigns(directory, clients):
    receipt = directory / 'business.json'
    proof = json.loads(receipt.read_bytes())
    release = directory.parent / 'serve-response-include-projection-20261128' / 'delivery.json'
    delivery = capture(release, proof['delivery_sha256'])
    publication = delivery['publication']
    capacity = capture(release.with_name('capacity.json'), delivery['capacity_receipt_sha256'])
    run, lease = proof['runtime'], proof['lease']
    captures = proof['captures']
    require(publication['status'] == proof['status'] == run['status'] == 'PASS'
            and publication['publication_revision'] == proof['revision']
            and publication['engine'] == proof['engine'] and publication['primary'] == proof['manifest']
            and capacity['engine'] == publication['engine'] and capacity['ingress'] == proof['ingress']
            and lease['status'] == 'COMPLETED' and lease['child_returncode'] == 0
            and lease['compute_backend'] == 'nvml' and lease['lease_inherited_by_child']
            and lease['successful_samples'] > 0 and run['model_exit'] == 0 and run['starts'] == 1
            and run['ports_free'] and run['unchanged_default'] and run['requests'] == 7 * len(clients)
            and set(captures) == set(clients), 'Issued model inference release or runtime differs')
    config = capture(directory / 'sdk-wire/scenario.json', proof['scenario_sha256'])
    revision = proof['revision']
    require(config['documents'] == [f'The q4t primary package has publication revision {revision}.',
                f'The independent fallback package manifest is {publication["fallback"]}.']
            and f'publication_revision: {revision}\n' in config['completion']['prompt'],
            'SDK scenario no longer reads the issued release')
    digest = sha(receipt)
    observed = set()
    for sdk in clients:
        path = directory / 'sdk-wire' / (sdk + '.json')
        actor = capture(path, captures[sdk])
        traces = verify(actor, config, sdk)
        require(not observed.intersection(traces), 'Reused inference request ID across SDKs')
        observed.update(traces)
        wires = actor['requests']
        statuses = Counter(str(row['status']) for row in wires)
        require(statuses == {'200': 6, '404': 1}, 'Missing inference success or unknown-model denial')
        yield 'inference-issued-' + sdk, [(sdk, row) for row in wires], statuses, dict(
            sdk=sdk, revision=revision, manifest=publication['primary'], engine=publication['engine'],
            ingress=proof['ingress'], transport='authenticated native TLS', requests=len(wires),
            receipt=f'{directory.name}/business.json', receipt_sha256=digest,
            wire=f'{directory.name}/sdk-wire/{path.name}', wire_sha256=captures[sdk])
