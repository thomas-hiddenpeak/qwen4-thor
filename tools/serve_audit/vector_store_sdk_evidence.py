"""Tie each vector store SDK exchange to its client, project documents and serving package."""
import json
from urllib.parse import parse_qsl

from .previous_page_receipt import verify as verify_pages
from .producer_sdk_evidence import capture, require
from .render_sdk_matrix import sha
from .vector_store_name_receipt import verify as verify_names
from .vector_store_recovery_receipt import verify as verify_recovery

SDK = 'python-3.55.1'
STORES = '/v1/vector_stores'
FILES = '/v1/files'


def check_calls(rows, expected):
    require(len(rows) == len(expected), 'Incomplete vector store exchange')
    for row, (method, path, query) in zip(rows, expected, strict=True):
        params = parse_qsl(row['query'], keep_blank_values=True, strict_parsing=True)
        require((row['method'], row['path'], row['status']) == (method, path, 200)
                and sorted(params) == sorted(query.items()) and row['trace'],
                'Vector store method, resource, result or query differs')


def device_calls(wire, proof, clients, documents, revision):
    seed, chat, cleanup = (wire[k] for k in ('setup', 'chat', 'cleanup'))
    require(set(wire['clients']) == set(wire['name_updates']) == set(clients)
            and seed['records'] == documents == proof['document_sha256']
            and seed['download_sha256'] == proof['download_sha256'] == documents['delivery.json']
            and chat['answer'].strip() == str(revision) == proof['chat'].strip()
            and chat['source_revision'] == seed['revision'] == revision
            and chat['usage']['prompt_tokens'] > 0 and chat['usage']['completion_tokens'] > 0
            and seed['sdk'] == chat['sdk'] == cleanup['sdk'] == SDK,
            'SDK, downloaded source identity or live answer differs')
    stores, files = seed['stores'], seed['files']
    require(len(stores) == len(set(stores)) == len(files) == len(set(files)) == 5
            and seed['store'] in stores and cleanup['deleted_stores'] == cleanup['deleted_files'] == 5,
            'Project stores, files or cleanup count differs')
    run = proof['runtime']
    require(proof['status'] == run['status'] == 'PASS' and run['starts'] == 1
            and run['model_exit'] == 0 and run['ports_free'] and run['unchanged_default']
            and proof['nvml_samples'] > 0 and proof['sdk_reads'] == 12 * len(clients)
            and proof['name_sdk_calls'] == 4 * len(clients), 'Incomplete device qualification')

    rows = seed['requests']
    base = STORES + '/' + seed['store']
    batch = base + '/file_batches'
    job = batch + '/' + seed['batch']
    listed = {'order': 'asc', 'limit': '100'}
    polls = len(rows) - 15
    require(1 <= polls and len(rows) == proof['seed_requests'] <= 65, 'Unexpected batch polling')
    path = rows[-1]['path']
    require(path in [FILES + '/' + ident + '/content' for ident in files],
            'Download came from a different file')
    check_calls(rows, [('POST', STORES, {}), ('POST', FILES, {})] * 5
                + [('POST', batch, {})] + [('GET', job, {})] * polls
                + [('GET', STORES, listed), ('GET', base + '/files', dict(listed, filter='completed')),
                   ('GET', job + '/files', dict(listed, filter='completed')), ('GET', path, {})])
    check_calls(chat['requests'], [('POST', '/v1/chat/completions', {})])
    check_calls(cleanup['requests'], [('DELETE', STORES + '/' + ident, {}) for ident in stores]
                + [('DELETE', FILES + '/' + ident, {}) for ident in files]
                + [('GET', STORES, {'limit': '1'}), ('GET', FILES, {'limit': '1'})])
    setup = rows + chat['requests'] + cleanup['requests']
    require(len(cleanup['requests']) == proof['cleanup_requests']
            and len(setup) + 16 * len(clients) == run['requests'],
            'Device request total differs')
    yield 'vector-store-lifecycle', SDK, setup
    for client in clients:
        reads, updates = wire['clients'][client], wire['name_updates'][client]
        verify_pages(reads, seed, client)
        verify_names(updates, seed['store'], client)
        yield 'vector-store-pages-and-name-' + client, client, reads['requests'] + updates['requests']


def recovered_calls(managed, publication, documents):
    require(managed['status'] == 'PASS' and managed['stock_sdk'] == SDK
            and managed['engine'] == publication['engine'] and managed['model_starts'] == 4
            and set(managed['roles']) == {'primary', 'fallback'}
            and managed['roles']['primary']['registered_fallback'] == publication['fallback']
            and publication['primary'] != publication['fallback'], 'Incomplete vector store recovery')
    traces = set()
    for role, run in managed['roles'].items():
        incident = 'model_exit' if role == 'fallback' else 'primary_gateway_SIGABRT'
        require(run['manifest'] == publication[role] and run['incident'] == incident
                and run['model_starts'] == 2 and run['new_generation'] > run['old_generation']
                and run['guardian_same'] and run['nvml_samples'] > 0 and run['core_chat'] == '42'
                and run['ports_free'] and run['default_unchanged'] and not run['remaining'],
                'Managed incident, serving package or cleanup differs')
        previous = None
        for mode in ('prepare', 'restore'):
            data = run['requests'][mode]
            control, *rows = data['requests']
            require(data['stock_sdk_indices'] == list(range(1, 13)) and control['sdk'] is False
                    and (control['method'], control['path'], control['status'], control['query']) ==
                    ('GET', '/v1/capabilities', 200, ''), 'Non-SDK control or attribution differs')
            actor = dict(ids=data['ids'], sdk=data['sdk'], stock_sdk_indices=list(range(12)))
            checked = verify_recovery(mode, rows, actor, documents, previous)
            phase_traces = [control['trace'], *checked['traces']]
            require(checked['records'] == data['records'] and data['traces'] == phase_traces
                    and len(set(phase_traces)) == 13 and all(isinstance(t, str) and t for t in phase_traces)
                    and not traces.intersection(phase_traces), 'Replayed records or trace identity differs')
            traces.update(phase_traces)
            previous = checked
            selected = 'fallback' if mode == 'restore' else role
            generation = run['new_generation'] if mode == 'restore' else run['old_generation']
            yield 'vector-store-' + role + '-' + mode, rows, dict(
                manifest=publication[selected], generation=generation, incident=incident,
                transport=run['transport'], direct_http_controls=1)


def vector_store_campaigns(directory, clients):
    receipt = directory / 'delivery.json'
    delivery = json.loads(receipt.read_bytes())
    publication = delivery['publication']
    prior_file = directory / delivery['previous_delivery']
    prior = json.loads(prior_file.read_bytes())
    library_file = prior_file.parent / prior['previous_delivery']
    library = json.loads(library_file.read_bytes())
    hashes = delivery['sdk_evidence']
    device = capture(directory / 'device.json', hashes['device.json'])
    wire = capture(directory / 'device-wire.json', hashes['device-wire.json'])
    managed = capture(directory / 'managed.json', hashes['managed.json'])
    capacity = capture(directory / 'capacity.json', delivery['capacity_receipt_sha256'])
    previous = prior['publication']
    require(publication['status'] == previous['status'] == library['publication']['status'] == 'PASS'
            and publication['model_starts'] == 0 and device['wire'] == hashes['device-wire.json']
            and device['previous_revision'] == delivery['previous_revision'] == previous['publication_revision']
            and device['installed_manifest'] == previous['primary'] and device['previous_engine'] == previous['engine']
            and device['candidate_engine'] == publication['engine'] == capacity['engine']
            and device['ingress'] == capacity['ingress']
            and managed['package_source'] == delivery['signed_source']['head']
            and delivery['short_receipt_sha256'] == hashes['managed.json']
            and prior['previous_revision'] == library['publication']['publication_revision'],
            'Vector store release lineage or original device source differs')
    library_docs = {k: library['sdk_evidence'][k] for k in
                    ('cpu.json', 'device.json', 'capacity-business.json', 'capacity.json')}
    library_docs['delivery.json'] = sha(library_file)
    def source(file, sdk, rows, **context):
        return dict(revision=publication['publication_revision'], engine=publication['engine'],
                    registered_during_run=previous['publication_revision'], tested_before_registration=True,
                    receipt=f'{directory.name}/delivery.json', receipt_sha256=sha(receipt),
                    wire=f'{directory.name}/{file}', wire_sha256=hashes[file],
                    sdk=sdk, requests=len(rows), **context)
    for name, sdk, rows in device_calls(wire, device, clients, library_docs, prior['previous_revision']):
        yield name, [(sdk, r) for r in rows], {'200':len(rows)}, source(
            'device-wire.json', sdk, rows, transport='native TLS', ingress=device['ingress'])
    docs = {k:prior['sdk_evidence'][k] for k in
            ('cpu.json', 'device.json', 'capacity-business.json', 'capacity.json', 'managed.json')}
    for name, rows, context in recovered_calls(managed, publication, docs):
        yield name, [(SDK, r) for r in rows], {'200':11, '404':1}, source(
            'managed.json', SDK, rows, **context)
