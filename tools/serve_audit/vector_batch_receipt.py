"""Reconcile a stock batch upload helper with another SDK's actual HTTP reads."""
from collections import Counter
from urllib.parse import parse_qsl


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(writer, reader, *, documents, generate):
    store = writer['store']
    batch = writer['batch']
    expected_counts = dict(total=2, completed=2, in_progress=0, failed=0, cancelled=0)
    require((reader['store'], reader['batch']) == (store, batch) and
            reader['counts'] == writer['counts'] == expected_counts, 'batch identity or terminal counts')
    files = reader['files']
    page = reader['page_ids']
    require(len(documents) == len(files) == len(page) == len(set(page)) == 2 and
            set(files) == set(documents) and set(page) == set(files.values()), 'incomplete paged files')
    newest = max(documents, key=documents.get)
    require(reader['revision'] == documents[newest] and reader['selected'] == files[newest],
            'filtered search selected another document')
    require((generate and reader['tool_files'] == [files[newest]] and bool(reader['answer'])) or
            (not generate and reader['tool_files'] == [] and reader['answer'] is None),
            'model-backed file search evidence')

    prefix = '/v1/vector_stores/' + store
    target = prefix + '/file_batches/' + batch
    writes = writer['requests']
    polls = [row for row in writes if row['method'] == 'GET']
    require(1 <= len(polls) <= 50 and all(
        row['path'] == target and row['helper'] == 'true' for row in polls), 'stock SDK polling')
    required = Counter({('POST', '/v1/vector_stores'): 1, ('POST', '/v1/files'): 2,
                        ('POST', prefix + '/file_batches'): 1, ('GET', target): len(polls)})
    require(Counter((row['method'], row['path']) for row in writes) == required and
            all(row['status'] == 200 for row in writes), 'batch upload requests')

    reads = reader['requests']
    expected = [('GET', target, 200), ('GET', target + '/files', 200), ('GET', target + '/files', 200)]
    for file_id in page:
        expected += [('GET', '/v1/files/' + file_id, 200),
                     ('GET', prefix + '/files/' + file_id + '/content', 200),
                     ('POST', prefix + '/files/' + file_id, 200)]
    expected += [('POST', prefix + '/search', 200), ('GET', target, 404)]
    if generate:
        expected.append(('POST', '/v1/responses', 200))
    expected.append(('DELETE', prefix, 200))
    expected.extend(('DELETE', '/v1/files/' + file_id, 200) for file_id in page)
    expected.append(('GET', '/v1/files', 200))
    require([(r['method'], r['path'], r['status']) for r in reads] == expected, 'reader HTTP lifecycle')
    first = [('filter', 'completed'), ('limit', '1'), ('order', 'asc')]
    for actual, params in zip(reads[1:3], (first, first + [('after', page[0])])):
        require(sorted(parse_qsl(actual['query'], keep_blank_values=True)) == sorted(params),
                'SDK batch pagination cursor')
    require(parse_qsl(reads[-1]['query']) == [('purpose', 'assistants')], 'final owner listing')
    calls = writes + reads
    ids = {r['trace'] for r in calls}
    require(all(isinstance(value, str) and value.strip() for value in ids) and len(ids) == len(calls),
            'unique HTTP request IDs')
    return ids
