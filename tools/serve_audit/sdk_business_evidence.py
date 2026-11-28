"""Index authenticated SDK business exchanges by public operation and SDK version."""
from collections import Counter, defaultdict
import csv
import io
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl

from .completion_recovery_evidence import completion_recovery_campaigns
from .producer_sdk_evidence import issued_producer_campaigns
from .model_inference_evidence import model_inference_campaigns
from .projection_sdk_evidence import projection_campaigns
from .container_receipt import verify as verify_container
from .render_sdk_matrix import CATALOG, VERSIONS, sha
from .response_handoff_receipt import verify as verify_response_handoff
from .stored_chat_receipt import verify as verify_stored_chat
from .stream_sdk_evidence import stream_campaigns
from .transcription_sdk_evidence import issued_transcription_campaigns, transcription_campaigns

EVIDENCE = CATALOG.parents[1]
CLIENTS = tuple(f'{language}-{version}' for language, version in VERSIONS)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_bytes())


class Catalog:
    def __init__(self, rows):
        self.methods = []
        self.paths = defaultdict(dict)
        for verb, route, *_ in rows:
            path, _, query = route.partition('?')
            selector = tuple(sorted(parse_qsl(query, strict_parsing=True)))
            parts = tuple(path.strip('/').split('/'))
            choices = self.paths[verb].setdefault(parts, {})
            require(selector not in choices, f'duplicate operation: {verb} {route}')
            choices[selector] = route
            self.methods.append((verb, route))

    def match(self, wire):
        verb, path = wire['method'], wire['path']
        require(path.startswith('/v1/') and not any(c in path for c in '?#'),
                f'invalid capture target: {verb} {path}')
        parts = path[4:].split('/')
        require(all(parts), f'empty path segment: {path}')
        candidates = []
        for pattern, choices in self.paths.get(verb, {}).items():
            if len(pattern) != len(parts):
                continue
            fixed = {i for i, p in enumerate(pattern) if not p.startswith('{')}
            if all(pattern[i] == parts[i] for i in fixed):
                candidates.append((fixed, choices))
        require(candidates, f'no catalogue operation for {verb} {path}')
        # Prefer a fixed resource such as /agents/automations over /agents/{id}.
        best = [choices for fixed, choices in candidates
                if all(other <= fixed for other, _ in candidates)]
        require(len(best) == 1, f'ambiguous path template: {verb} {path}')
        choices = best[0]
        selector_keys = {key for query in choices for key, _ in query}
        if selector_keys and 'query' not in wire:
            return None
        values = parse_qsl(wire.get('query', ''), strict_parsing=True, keep_blank_values=True)
        selected = tuple(sorted((k, v) for k, v in values if k in selector_keys))
        require(selected in choices, f'uncatalogued selector: {verb} {path} {selected}')
        return verb, choices[selected]


def conversations(proof, runs):
    require(proof['strict_lease']['status'] == 'COMPLETED' and proof['cleanup']['model_pid_gone']
            and proof['cleanup']['ports_free'] and proof['model']['starts'] == 1,
            'conversation run or cleanup incomplete')
    require(len(runs) == len(CLIENTS) and set(proof['sdk']) == set(CLIENTS),
            'incomplete conversation SDK set')
    labels = [f"{r['language']}-{r['version']}" for r in runs]
    require(set(labels) == set(CLIENTS), 'conversation actors differ')
    packets = [(label, wire) for label, run in zip(labels, runs) for wire in run['requests']]
    return proof['release']['revision'], packets, proof['status_counts']


def uploads(proof, rows):
    require(proof['guard']['status'] == 'COMPLETED' and proof['runtime']['status'] == 'PASS'
            and proof['handoff']['guardian_same'] and proof['takeover']['fallback_consumed'],
            'missing managed upload recovery')
    packets = [(r['sdk'], r['wire']) for r in rows]
    require(Counter(label for label, _ in packets) == proof['sdk_calls'], 'upload SDK counts differ')
    return proof['publication'], packets, proof['verification']['codes']


def vectors(proof, pairs):
    require(proof['stage'] == 'issued_engine_and_native_embedding_verified'
            and proof['model_exit'] == 0 and proof['ports_free'], 'vector run incomplete')
    packets = []
    for pair, scenario in zip(pairs, proof['scenarios'], strict=True):
        for key, actor in [('write', 'writer'), ('read', 'reader')]:
            label = scenario[actor]
            require(label.split('-')[1] == pair[key]['version'] and
                    pair[key]['store'] == scenario['store'], 'vector actor or store differs')
            packets.extend((label, r) for r in pair[key]['requests'])
    require(len(packets) == proof['requests'], 'vector request count differs')
    return proof['publication'], packets, proof['statuses']


def project_receipt(proof, pair):
    receipt = EVIDENCE / pair['source']
    origin = proof['source_publications'][pair['source']]
    require(sha(receipt) == origin['delivery_sha256'], 'project publication receipt changed')
    published = read(receipt)['publication']
    raw = (json.dumps(published, indent=2) + '\n').encode()
    require(published['publication_revision'] == origin['publication'] and
            hashlib.sha256(raw).hexdigest() == origin['original_sha256'],
            'input does not reproduce the original publication bytes')
    return raw


def stored_chats(proof, pairs):
    result = proof['result']
    require(result['status'] == 'PASS' and result['starts'] == 1 and result['ports_free']
            and result['unchanged_default'] and result['model_exit'] == 0, 'stored Chat run incomplete')
    require(len(pairs) == len(CLIENTS) and proof['sdk_versions'] == list(CLIENTS),
            'incomplete stored Chat SDK set')
    packets = []
    seen_ids = set()
    for i, pair in enumerate(pairs):
        writer, reader = pair['writer'], pair['reader']
        require(writer['sdk'] == CLIENTS[i] and reader['sdk'] == CLIENTS[(i+1) % len(CLIENTS)],
                'stored Chat reader or writer differs')
        raw = project_receipt(proof, pair)
        verify_stored_chat(writer, reader, pair['request'], pair['metadata'], raw)
        ident = writer['created']['id']
        require(ident not in seen_ids, 'stored Chat ID reused between jobs')
        seen_ids.add(ident)
        for actor in (writer, reader):
            packets.extend((actor['sdk'], wire) for wire in actor['requests'])
    require(len(packets) == result['requests'] == proof['requests'], 'stored Chat request count differs')
    return proof['publication'], packets, proof['statuses']


def responses(proof, pairs):
    package = proof['issued_package']
    path = EVIDENCE / package['source']
    require(sha(path) == package['delivery_sha256'], 'issued Responses receipt differs')
    published = read(path)['publication']
    require(published['status'] == 'PASS' and published['engine'] == proof['engine']
            and published['primary'] == proof['primary'] and published['publication_revision'] == proof['publication'],
            'Responses engine or release differs')
    result = proof['result']
    require(result['status'] == 'PASS' and result['starts'] == 1 and result['ports_free']
            and result['unchanged_default'] and result['model_exit'] == 0, 'Responses device run incomplete')
    require(len(pairs) == len(CLIENTS) and proof['sdk_versions'] == list(CLIENTS), 'incomplete Responses SDK set')
    packets = []
    ids = set()
    for i, pair in enumerate(pairs):
        writer, reader = pair['writer'], pair['reader']
        require(writer['sdk'] == CLIENTS[i] and reader['sdk'] == CLIENTS[(i+1) % len(CLIENTS)],
                'Responses writer or reader differs')
        verify_response_handoff(writer, reader, pair['request'], pair['followup'], project_receipt(proof, pair))
        for ident in (writer['created']['id'], reader['follow']['id']):
            require(ident not in ids, 'response identity reused')
            ids.add(ident)
        for actor in (writer, reader):
            packets.extend((actor['sdk'], wire) for wire in actor['requests'])
    require(len(ids) == proof['distinct_response_ids'] and len(packets) == result['requests'] == proof['requests'],
            'Responses identities or HTTP totals differ')
    return proof['publication'], packets, proof['statuses']


def issued_containers(proof, records):
    published = read(EVIDENCE / 'serve-interpreter-output-discovery-20261128/delivery.json')['publication']
    require(published == proof['registration'] and published['status'] == 'PASS'
            and published['primary'] == proof['primary'] and published['engine'] == proof['engine']
            and published['publication_revision'] == proof['publication'], 'container release differs')
    guard, run = proof['strict_lease'], proof['result']
    require(guard['status'] == 'COMPLETED' and guard['inherited'] and guard['backend'] == 'nvml'
            and guard['returncode'] == 0 and guard['samples'] > 0 and run['status'] == 'PASS'
            and run['model_exit'] == 0 and run['starts'] == 1 and run['ports_free']
            and run['unchanged_default'], 'container device run incomplete')
    document = EVIDENCE / 'serve-memory-watchdog-stall-20261128/delivery.json'
    require(proof['document']['path'] == str(document.relative_to(EVIDENCE.parents[1]))
            and sha(document) == proof['document']['sha256']
            and document.stat().st_size == proof['document']['bytes'], 'container document differs')
    require(set(records) == set(CLIENTS) and proof['sdk_versions'] == list(CLIENTS),
            'container SDK set differs')
    packets = []
    body = document.read_bytes()
    for client in CLIENTS:
        record = records[client]
        verify_container(client, record, body)
        packets.extend((client, row) for row in record['requests'])
    require(len(packets) == proof['requests'] == run['requests'] == 90, 'container request total differs')
    return proof['publication'], packets, proof['statuses']


# Only suites with original, authenticated wire records and a matching device receipt.
SUITES = (
    ('containers', 'serve-container-sdk-issued-20261128', 'request_evidence_sha256', issued_containers),
    ('conversations', 'serve-conversation-sdk-business-20261128', 'requests_sha256', conversations),
    ('uploads', 'serve-upload-sdk-recovery-20261128', 'trace_sha256', uploads),
    ('vectors', 'serve-vector-batch-sdk-20261128', 'request_evidence_sha256', vectors),
    ('chats', 'serve-stored-chat-sdk-20261128', 'request_evidence_sha256', stored_chats),
    ('responses', 'serve-responses-sdk-business-20261128', 'request_evidence_sha256', responses),
)


def index(catalog, suites):
    traces = set()
    counts = defaultdict(Counter)
    unassigned = []
    for name, (revision, packets, expected) in suites:
        codes = Counter()
        for client, wire in packets:
            code, trace = wire['status'], wire['trace']
            require(client in CLIENTS and type(code) is int and 200 <= code < 600,
                    'unknown SDK or invalid final HTTP status')
            require(isinstance(trace, str) and re.fullmatch(r'\S+', trace) and trace not in traces,
                    'missing or repeated HTTP request ID')
            traces.add(trace)
            codes[str(code)] += 1
            method = catalog.match(wire)
            if method is None:
                unassigned.append(dict(suite=name, revision=revision, sdk=client,
                                       trace=trace, method=wire['method'], path=wire['path'],
                                       status=code, reason='query not present in wire receipt'))
            else:
                counts[method, client][name, code] += 1
        require(codes == expected, f'{name}: HTTP status counts differ from device receipt')
    return counts, unassigned


def recovered_batches(sources, suites):
    """Index each SDK actor on its own side of the recorded recovery boundary."""
    directory = EVIDENCE / 'serve-batch-cancel-20261128'
    managed_path = directory / 'managed.json'
    delivery_path = directory / 'delivery.json'
    managed, delivery = read(managed_path), read(delivery_path)
    publication = delivery['publication']
    require(sha(managed_path) == delivery['short_receipt_sha256'] and publication['status'] == 'PASS'
            and managed['engine'] == publication['engine'], 'Batch recovery release differs')
    for role, incident in [('fallback', 'model'), ('primary', 'gateway')]:
        claim = managed['roles'][role]
        require(claim['manifest'] == publication[role] and claim['starts'] == 2 and claim['guardian_same']
                and claim['new_generation'] > claim['old_generation'] and claim['ports_released']
                and claim['default_unchanged'] and not claim['remaining'], 'incomplete Batch recovery')
        if role == 'primary':
            require(claim['registered_fallback'] == publication['fallback'], 'Batch recovery fallback differs')
        total = 0
        for phase in ('submit', 'checkpoint', 'restore'):
            name = f'batch-{incident}-{phase}'
            leaf = phase + '-wire.json'
            wire_path = directory / 'sdk-wire' / role / leaf
            digest = claim['wire_sha256'][leaf]
            require(sha(wire_path) == digest, 'original Batch recovery capture differs')
            wires = read(wire_path)
            sdk = claim['sdk'][1 if phase == 'restore' else 0]
            codes = Counter(str(w['status']) for w in wires)
            revision = publication['publication_revision']
            suites.append((name, (revision, [(sdk, w) for w in wires], codes)))
            after = phase == 'restore'
            serving = claim['registered_fallback'] if after and role == 'primary' else claim['manifest']
            sources[name] = dict(revision=revision, transport='authenticated native loopback HTTP',
                                 requests=len(wires), sdk=sdk, incident=claim['incident'],
                                 phase='after_recovery' if after else 'before_incident',
                                 generation=claim['new_generation'] if after else claim['old_generation'],
                                 observed='qualified candidate before issuance', initial_manifest=claim['manifest'], manifest=serving,
                                 receipt=str(managed_path.relative_to(EVIDENCE)), receipt_sha256=sha(managed_path),
                                 wire=str(wire_path.relative_to(EVIDENCE)), wire_sha256=digest)
            total += len(wires)
        require(total == claim['requests'], 'Batch recovery actor totals differ')


def render():
    catalog = Catalog(read(CATALOG)['rows'])
    sources, suites = {}, []
    for name, directory, digest_key, unpack in SUITES:
        folder = EVIDENCE / directory
        receipt, trace = folder / 'business.json', folder / 'requests.json'
        proof = read(receipt)
        digest = sha(trace)
        require(digest == proof[digest_key], f'{name}: original capture digest differs')
        revision, packets, statuses = unpack(proof, read(trace))
        suites.append((name, (revision, packets, statuses)))
        sources[name] = dict(revision=revision, transport=proof['transport'], requests=len(packets),
                             receipt=f'{directory}/business.json', receipt_sha256=sha(receipt),
                             wire=f'{directory}/requests.json', wire_sha256=digest)
    recovered_batches(sources, suites)
    for name, packets, source in stream_campaigns(
            EVIDENCE / 'serve-responses-search-stream-sdk-20261128', CLIENTS):
        sources[name] = source
        suites.append((name, (source['revision'], packets, {'200': source['requests']})))
    speech = EVIDENCE / 'serve-transcription-discovery-20261128'
    for campaigns in (transcription_campaigns(speech), issued_transcription_campaigns(speech, CLIENTS)):
        for name, packets, source in campaigns:
            sources[name] = source
            suites.append((name, (source['revision'], packets, {'200': source['requests']})))
    producers = EVIDENCE / 'serve-interpreter-output-discovery-20261128'
    for name, packets, source in issued_producer_campaigns(producers, CLIENTS):
        sources[name] = source
        suites.append((name, (source['revision'], packets, {'200': 3, '404': 1})))
    for name, packets, statuses, source in projection_campaigns(
            EVIDENCE / 'serve-response-include-projection-20261128'):
        sources[name] = source
        suites.append((name, (source['revision'], packets, statuses)))
    for name, packets, statuses, source in model_inference_campaigns(
            EVIDENCE / 'serve-model-inference-sdk-20261128', CLIENTS):
        sources[name] = source
        suites.append((name, (source['revision'], packets, statuses)))
    for name, packets, statuses, source in completion_recovery_campaigns(
            EVIDENCE / 'serve-completion-best-of-20261128'):
        sources[name] = source
        suites.append((name, (source['revision'], packets, statuses)))
    counts, unassigned = index(catalog, suites)
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(['method', 'path', *CLIENTS])
    for method in catalog.methods:
        cells = []
        for client in CLIENTS:
            cell = counts.get((method, client), {})
            cells.append(' '.join(f'{name}/{code}={n}' for (name, code), n in sorted(cell.items())))
        writer.writerow([*method, *cells])
    positive = {(method, client) for (method, client), cell in counts.items()
                if any(200 <= code < 300 for _, code in cell)}
    summary = dict(method_catalog_sha256=sha(CATALOG), sources=sources,
                   methods=len(catalog.methods), sdk_versions=len(CLIENTS),
                   source_requests=sum(v['requests'] for v in sources.values()),
                   assigned_requests=sum(sum(cell.values()) for cell in counts.values()),
                   methods_with_success=len({method for method, _ in positive}),
                   method_sdk_pairs_with_success=len(positive), unassigned=unassigned)
    return buffer.getvalue(), summary


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    output = parser.parse_args().output
    table, manifest = render()
    output.mkdir(parents=True, exist_ok=True)
    (output / 'business.csv').write_text(table)
    (output / 'sources.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
