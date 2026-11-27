"""Verify the wire targets actually used by a conversation SDK lifecycle."""
import re
from urllib.parse import parse_qsl


def verify(receipt, *, language, version, expected_answer=None):
    if (receipt['language'], receipt['version'], receipt['answer']) != (
            language, version, expected_answer):
        raise ValueError('SDK identity or project answer differs')
    conversation = receipt['conversation']
    items = receipt['items']
    if not re.fullmatch(r'conv_[A-Za-z0-9_-]+', conversation):
        raise ValueError('invalid conversation identity')
    if len(items) != 2 or len(set(items)) != 2 or not all(
            re.fullmatch(r'[A-Za-z0-9_-]+', item) for item in items):
        raise ValueError('two distinct item identities required')
    root = '/v1/conversations'
    resource = root + '/' + conversation
    collection = resource + '/items'
    first, last = [collection + '/' + item for item in items]
    page = [('limit', '1'), ('order', 'asc')]
    expected = [
        ('POST', root, 200, []), ('POST', resource, 200, []),
        ('GET', resource, 200, []), ('POST', collection, 200, []),
        ('GET', collection, 200, page),
        ('GET', collection, 200, page + [('after', items[0])]),
        ('GET', last, 200, []), ('GET', last, 404, []),
    ]
    if expected_answer is not None:
        expected.append(('POST', '/v1/responses', 200, []))
    expected.extend([
        ('DELETE', first, 200, []), ('GET', first, 404, []),
        ('DELETE', resource, 200, []), ('GET', resource, 404, []),
    ])
    traces = set()
    for row, (verb, path, status, query) in zip(receipt['requests'], expected,
                                               strict=True):
        sent_query = parse_qsl(row['query'], strict_parsing=True, keep_blank_values=True)
        if (row['method'], row['path'], row['status'], sorted(sent_query)) != (
                verb, path, status, sorted(query)):
            raise ValueError(f'unexpected request or response for {verb} {path}')
        trace = row['trace']
        if not isinstance(trace, str) or not trace.strip() or trace in traces:
            raise ValueError('each live HTTP exchange needs its own request ID')
        traces.add(trace)
    return traces
