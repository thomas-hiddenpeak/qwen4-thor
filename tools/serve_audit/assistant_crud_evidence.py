"""Bind typed Assistants CRUD and the follow-up Chat to one registered release."""
import json

from .assistant_crud_profile import verify
from .producer_sdk_evidence import capture, require
from .render_sdk_matrix import sha


def read_clients(directory, clients, digests, document):
    require(set(digests) == set(clients), 'Different SDK versions in Assistants receipt')
    ids, traces = set(), set()
    for client in clients:
        wire = capture(directory / (client + '.json'), digests[client])
        checked = verify(wire, client, document)
        used = set(wire['ids'].values())
        require(len(used) == 3 and ids.isdisjoint(used) and traces.isdisjoint(checked),
                'An Assistants resource or request belongs to two SDK campaigns')
        traces.update(checked)
        ids.update(used)
        yield client, wire


def verify_chat(chat, actor, publication):
    sdk = 'python-3.55.1'
    received = next(r for r in actor['requests'] if r['step'] == 'message-read')
    excerpt = received['response']['content'][0]['text']['value']
    fields = json.loads(excerpt)
    require(fields == {k:publication[k] for k in ('desired','publication_revision','engine')},
            'Chat input refers to a different release')
    wire = chat['request']
    require(chat['sdk'] == sdk == actor['sdk'] and wire['trace']
            and (wire['method'], wire['path'], wire['status']) == ('POST', '/v1/chat/completions', 200),
            'The follow-up was not a successful typed Chat request')
    body, result = wire['body'], wire['response']
    require(body['temperature'] == 0 and body['model'] == 'qwen3.8-flash-next'
            and len(body['messages']) == 1 and body['messages'][0]['role'] == 'user'
            and body['messages'][0]['content'].partition('\n')[2] == excerpt
            and chat['answer'] == str(publication['publication_revision'])
            and len(result['choices']) == 1 and result['choices'][0]['finish_reason'] == 'stop'
            and result['choices'][0]['message']['content'].strip() == chat['answer']
            and result['usage']['prompt_tokens'] > 0 and result['usage']['completion_tokens'] > 0,
            'Chat must answer from the original thread message with real token usage')
    return wire


def assistant_campaigns(directory, clients):
    receipt = directory / 'device.json'
    proof = json.loads(receipt.read_bytes())
    release = directory / proof['release']
    issued = capture(release, proof['release_sha256'])
    publication = issued['publication']
    capacity = capture(release.with_name('capacity.json'), issued['capacity_receipt_sha256'])
    run, guard = proof['runtime'], proof['guard']
    require(proof['status'] == publication['status'] == run['status'] == 'PASS'
            and proof['revision'] == publication['publication_revision']
            and proof['manifest'] == publication['primary']
            and proof['engine'] == publication['engine'] == capacity['engine']
            and proof['ingress'] == capacity['ingress']
            and run['starts'] == 1 and run['requests'] == 20 * len(clients) + 1
            and run['model_exit'] == 0
            and run['ports_free'] and run['unchanged_default']
            and guard['status'] == 'COMPLETED' and guard['backend'] == 'nvml'
            and guard['samples'] > 0 and guard['inherited'] and guard['child_returncode'] == 0,
            'Assistants receipt lacks a successful registered-engine run')
    source_bytes = release.read_bytes()
    digests = proof['sdk_wire_sha256']
    observed = {}
    def campaign(name, sdk, wires, filename, digest, codes):
        meta = dict(revision=proof['revision'], sdk=sdk, requests=len(wires),
                    engine=proof['engine'], manifest=proof['manifest'], ingress=proof['ingress'],
                    transport='authenticated native TLS', observed='issued engine and ingress',
                    receipt=directory.name+'/device.json', receipt_sha256=sha(receipt),
                    wire=directory.name+'/device-wire/'+filename, wire_sha256=digest)
        return name, [(sdk, wire) for wire in wires], codes, meta

    for sdk, actor in read_clients(directory/'device-wire', clients, digests, source_bytes):
        observed[sdk] = actor
        yield campaign('assistants-crud-'+sdk, sdk, actor['requests'], sdk+'.json',
                       digests[sdk], {'200':14, '404':6})
    sdk = 'python-3.55.1'
    chat = capture(directory/'device-wire/chat.json', proof['chat_sha256'])
    wire = verify_chat(chat, observed[sdk], publication)
    require(all(wire['trace'] != row['trace'] for a in observed.values() for row in a['requests']),
            'Chat request ID collides with Assistants traffic')
    yield campaign('assistants-release-readback', sdk, [wire], 'chat.json',
                   proof['chat_sha256'], {'200':1})
