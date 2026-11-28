"""Check both raw JSON and SDK results for a cleared vector store name."""
from urllib.parse import parse_qsl


def verify(record, store, sdk):
    if record['store'] != store or record['sdk'] != sdk:
        raise ValueError('Vector store or SDK differs')
    title = 'Thor 项目资料'
    expected = [title, '', '', '']
    if record['names'] != expected or len(record['requests']) != len(expected):
        raise ValueError('SDK did not return a string for the cleared name')
    resource = '/v1/vector_stores'
    traces = set()
    for idx, (row, value) in enumerate(zip(record['requests'], expected, strict=True)):
        method = 'POST' if idx < 2 else 'GET'
        path = resource if idx == 3 else resource + '/' + store
        query = [('limit', '100')] if idx == 3 else []
        body = [{'name': title}, {'name': None}, None, None][idx]
        if (row['method'] != method or row['path'] != path or row['status'] != 200
                or parse_qsl(row['query'], keep_blank_values=True, strict_parsing=True) != query
                or row['body'] != body or row['name'] != value):
            raise ValueError('Vector store update, lookup, list or wire name differs')
        trace = row['trace']
        if not isinstance(trace, str) or not trace.strip() or trace in traces:
            raise ValueError('Missing or repeated vector store request ID')
        traces.add(trace)
    return traces
