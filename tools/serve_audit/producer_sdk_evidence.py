"""Attribute interpreter calls and stored reads to the stock SDK that sent them."""
import json
from urllib.parse import parse_qsl

from .render_sdk_matrix import sha


def require(condition, message):
    if not condition:
        raise ValueError(message)


def capture(path, digest):
    require(sha(path) == digest, f"Producer evidence changed at {path.name}")
    return json.loads(path.read_bytes())


def issued_producer_campaigns(directory, clients):
    delivery = json.loads((directory / 'delivery.json').read_bytes())
    publication = delivery['publication']
    digest = delivery['sdk_evidence']['clients.json']
    proof = capture(directory / 'clients.json', digest)
    capacity = capture(directory / 'capacity.json', delivery['capacity_receipt_sha256'])
    run = proof['runtime']
    guard = proof['guard']
    require(publication['status'] == proof['status'] == run['status'] == 'PASS'
            and publication['publication_revision'] == proof['publication']
            and publication['primary'] == proof['package'] and publication['engine'] == proof['engine']
            and proof['ingress'] == capacity['ingress'] and capacity['engine'] == proof['engine']
            and guard['status'] == 'COMPLETED' and guard['backend'] == 'nvml'
            and guard['child_returncode'] == 0 and guard['samples'] > 0 and run['starts'] == 1
            and run['model_exit'] == 0 and run['ports_free'] and run['unchanged_default']
            and set(proof['sdk']) == set(clients) and run['requests'] == 4 * len(clients),
            'Published interpreter run or cleanup differs')
    responses = set()
    containers = set()
    for sdk in clients:
        summary = proof['sdk'][sdk]
        path = directory / 'sdk-wire' / (sdk + '.json')
        actor = capture(path, summary['capture'])
        events = actor['events']
        terminal = events[-1]['response']
        rid = terminal['id']
        require(actor['sdk'] == sdk and rid == actor['stored']['id'] == summary['response']
                and rid not in responses and actor['container'] == summary['container']
                and actor['container'] not in containers
                and actor['requests'] == summary['requests']
                and [v['sequence_number'] for v in events] == list(range(len(events)))
                and len(events) == summary['events'] and events[-1]['type'] == 'response.completed'
                and terminal['status'] == 'completed' and terminal['output'] == actor['stored']['output'],
                'Interpreter stream, stored response or SDK identity changed')
        responses.add(rid)
        containers.add(actor['container'])
        calls = [x for x in terminal['output'] if x['type'] == 'code_interpreter_call']
        done = [e['item'] for e in events if e['type'] == 'response.output_item.done']
        text = ''.join(part['text'] for item in terminal['output'] if item['type'] == 'message'
                       for part in item['content'] if part['type'] == 'output_text')
        require(len(calls) == 1 and calls[0]['id'] == summary['item'] and calls[0] in done
                and calls[0]['container_id'] == actor['container'] and calls[0]['status'] == 'completed'
                and calls[0]['outputs'] == [dict(type='logs', logs='603\n')]
                and summary['stdout'] == '603\n' and text.strip() == '603'
                and any(e['type'] == 'response.code_interpreter_call.completed' for e in events),
                'Interpreter output, container or completed event differs')
        endpoint = '/v1/responses/' + rid
        wire = actor['requests']
        require([(r['method'], r['path'], r['status']) for r in wire] ==
                [('POST','/v1/containers',200), ('POST','/v1/responses',200),
                 ('GET',endpoint,200), ('GET',endpoint,404)], 'Interpreter client methods differ')
        args = [parse_qsl(r['query'], strict_parsing=True, keep_blank_values=True) for r in wire]
        require(args[:2] == [[], []] and args[2] in
                ([('include[]','code_interpreter_call.outputs')], [('include','code_interpreter_call.outputs')])
                and args[3] == [], 'Interpreter client selectors differ')
        yield 'interpreter-issued-' + sdk, [(sdk, row) for row in wire], dict(
            sdk=sdk, revision=proof['publication'], manifest=proof['package'], engine=proof['engine'],
            ingress=proof['ingress'], requests=len(wire), transport='authenticated native TLS',
            receipt=f'{directory.name}/clients.json', receipt_sha256=digest,
            wire=f'{directory.name}/sdk-wire/{path.name}', wire_sha256=summary['capture'])
