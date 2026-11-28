"""Check a stock SDK's container lifecycle against its observed HTTP exchanges."""
from hashlib import sha256
from urllib.parse import parse_qsl


def verify(label, result, document):
    def require(condition, message):
        if not condition:
            raise ValueError(f'{label}: {message}')

    require(f"{result['language']}-{result['version']}" == label, 'SDK identity differs')
    require((result['bytes'], result['sha256']) == (len(document), sha256(document).hexdigest()),
            'download differs from the project document')
    box_id = result['container']
    files = result['files']
    pages = result['pages']
    require(isinstance(box_id, str) and box_id and '/' not in box_id, 'invalid container ID')
    require(len(files) == len(pages) == len(set(files)) == 2 and set(files) == set(pages)
            and all(isinstance(i, str) and i and '/' not in i for i in files), 'invalid file pages')
    collection = '/v1/containers'
    box = f'{collection}/{box_id}'
    listing = box + '/files'
    file = f'{listing}/{files[0]}'
    expected = [('POST', collection, 200), ('GET', collection, 200), ('GET', box, 200),
                ('POST', listing, 200), ('POST', listing, 200), ('GET', listing, 200),
                ('GET', listing, 200), ('GET', file, 200), ('GET', file + '/content', 200),
                ('GET', box, 404), ('GET', file + '/content', 404), ('DELETE', file, 200),
                ('GET', file, 404), ('DELETE', box, 200), ('GET', box, 404)]
    rows = result['requests']
    require([(r['method'], r['path'], r['status']) for r in rows] == expected,
            'unexpected method, path or status')
    params = {1: [('limit', '100')], 5: [('limit', '1'), ('order', 'asc')],
              6: [('limit', '1'), ('order', 'asc'), ('after', pages[0])]}
    for index, row in enumerate(rows):
        actual = parse_qsl(row['query'], keep_blank_values=True, strict_parsing=True)
        require(sorted(actual) == sorted(params.get(index, [])), 'query or page cursor differs')
    traces = {r['trace'] for r in rows}
    require(len(traces) == len(rows) and all(isinstance(t, str) and t for t in traces),
            'missing or repeated request ID')
    return traces
