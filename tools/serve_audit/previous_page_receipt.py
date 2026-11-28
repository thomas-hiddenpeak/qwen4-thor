"""Verify backward SDK reads against the initial ordered store and file IDs."""
from urllib.parse import parse_qsl


def verify(record, seed, client):
    prefix = '/v1/vector_stores'
    store = prefix + '/' + seed['store']
    targets = {'stores': prefix, 'files': store + '/files',
               'batch_files': store + '/file_batches/' + seed['batch'] + '/files'}
    if record['sdk'] != client or len(record['requests']) != 12 or len(record['pages']) != 12:
        raise ValueError('Unexpected SDK or number of pages')
    pairs = iter(zip(record['requests'], record['pages'], strict=True))
    traces = set()
    for resource, path in targets.items():
        original = seed['stores' if resource == 'stores' else 'files']
        if len(original) != 5 or len(set(original)) != 5:
            raise ValueError('Expected five unique ordered source IDs')
        for order in ('asc', 'desc'):
            ids = original if order == 'asc' else original[::-1]
            for end in (4, 2):
                request, page = next(pairs)
                expected = {'before': ids[end], 'order': order, 'limit': '2'}
                if resource != 'stores':
                    expected['filter'] = 'completed'
                query = parse_qsl(request['query'], keep_blank_values=True, strict_parsing=True)
                if ((request['method'], request['path'], request['status']) != ('GET', path, 200)
                        or sorted(query) != sorted(expected.items())):
                    raise ValueError('Incorrect previous-page request or HTTP result')
                if (page != dict(resource=resource, order=order, before=ids[end],
                                 ids=ids[end-2:end], first=ids[end-2], last=ids[end-1], more=end > 2)
                        or type(page['more']) is not bool):
                    raise ValueError('Previous page skipped rows or changed order, cursors or has_more')
                trace = request['trace']
                if not isinstance(trace, str) or not trace.strip() or trace in traces:
                    raise ValueError('Missing or repeated request ID')
                traces.add(trace)
    return traces
