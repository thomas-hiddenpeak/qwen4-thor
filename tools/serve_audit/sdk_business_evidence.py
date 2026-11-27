"""Index authenticated SDK business exchanges by public operation and SDK version."""
from collections import Counter, defaultdict
import csv
import io
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl

from .render_sdk_matrix import CATALOG, VERSIONS, sha

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


# Only suites with original, authenticated wire records and a matching device receipt.
SUITES = (
    ('conversations', 'serve-conversation-sdk-business-20261128', 'requests_sha256', conversations),
    ('uploads', 'serve-upload-sdk-recovery-20261128', 'trace_sha256', uploads),
    ('vectors', 'serve-vector-batch-sdk-20261128', 'request_evidence_sha256', vectors),
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
