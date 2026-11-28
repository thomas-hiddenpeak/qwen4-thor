"""Verify the Batch transition, two SDK identities and the successful project row."""
from collections import Counter
import hashlib
import json
from urllib.parse import parse_qsl, urlsplit


def check_loopback(config):
    target = urlsplit(config['base'])
    scheme = 'http' if config.get('managed_loopback') is True else 'https'
    if (target.scheme != scheme or target.hostname != '127.0.0.1' or not target.port
            or target.username is not None or target.password is not None
            or target.path != '/v1' or target.query or target.fragment):
        raise ValueError('expected the selected loopback SDK endpoint')


def batch_input(document, model, label, count):
    body = dict(model=model, temperature=0, reasoning_effort='none',
                max_completion_tokens=48, messages=[
                    dict(role='system', content='Answer with only the requested integer.'),
                    dict(role='user', content='What is the publication_revision of this q4t release?\n'+document)])
    rows = (dict(custom_id=f'{label}-{i}', method='POST', url='/v1/chat/completions', body=body)
            for i in range(count))
    return ''.join(json.dumps(row)+'\n' for row in rows).encode()


def verify(writer, reader, document, model, *, callers=4):
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    require(type(callers) is int and callers in (2, 4), 'invalid caller count')
    revision = json.loads(document)['publication_revision']
    require(writer['sdk'].startswith('python-') and reader['sdk'].startswith('node-'),
            'two different SDK actors required')
    require(writer['revision'] == revision, 'source publication differs')
    done, held = writer['completed'], writer['pending']
    finished, stopped = reader['finished'], reader['stopped']
    require(done['id'] != held['id'] and done['input'] != held['input'], 'duplicate Batch or File ID')
    for job, terminal, label, count, status in [(done, finished, 'publication', 1, 'completed'),
                                                (held, stopped, 'cancel', 12, 'cancelled')]:
        digest = hashlib.sha256(batch_input(document,model,label,count)).hexdigest()
        metadata = dict(project='q4t', scenario=label, revision=str(revision))
        require(job['rows'] == count and job['source_sha256'] == digest, 'submitted project rows differ')
        require(terminal['id'] == job['id'] and terminal['input_file_id'] == job['input']
                and terminal['status'] == status and terminal['metadata'] == job['metadata'] == metadata,
                'terminal Batch identity or metadata differs')
    cancel_time = stopped['cancelling_at']
    require(type(cancel_time) is int and cancel_time > 0 and len(writer['cancel']) == callers and
            all(r['id'] == held['id'] and r['cancelling_at'] == cancel_time and
                r['status'] in ('cancelling','cancelled') for r in writer['cancel']),
            'callers did not observe one cancellation')
    require(stopped['cancelled_at'] >= cancel_time and reader['repeated'] == {
        'id': held['id'], 'status': 'cancelled', 'cancelled_at': stopped['cancelled_at'],
        'cancelling_at': cancel_time}, 'late cancellation changed the terminal receipt')
    require(finished['request_counts'] == dict(total=1, completed=1, failed=0), 'successful row count differs')
    output_lines = reader['output_text'].splitlines()
    require(len(output_lines) == 1 and json.loads(output_lines[0]) == reader['result'],
            'downloaded JSONL does not contain the recorded result')
    row = reader['result']
    require(row['custom_id'] == 'publication-0' and row['error'] is None and
            row['response']['status_code'] == 200 and row['response']['request_id'] and
            row['response']['body']['choices'][0]['message']['content'].strip() == str(revision) and
            reader['independent_chat'] == '42', 'Batch answer or independent Chat differs')
    pages = reader['page_ids']
    require(len(pages) == 2 and set(pages) == {done['id'], held['id']}, 'bad Batch list pagination')
    files = '/v1/files'
    batches = '/v1/batches'
    complete, pending = batches+'/'+done['id'], batches+'/'+held['id']
    cancel = pending+'/cancel'
    expected = Counter({('POST', files, 200):2, ('POST', batches, 200):2,
                        ('POST', cancel, 200):callers, ('POST', cancel, 404):1})
    def route(wire):return wire['method'], wire['path'], wire['status']
    requests = writer['requests']
    denied = [r for r in requests if route(r) == ('POST', cancel, 429)]
    if denied:
        expected['POST', cancel, 429] = len(denied)
    require(Counter(map(route, requests)) == expected, 'original SDK writes or ownership errors differ')
    require({r['client'] for r in writer['cancel']} == set(range(callers)), 'missing cancellation caller')
    first_attempts = []
    for client in range(callers):
        attempts = [r for r in requests if r.get('client') == client]
        require(1 <= len(attempts) <= 4 and
                [r['attempt'] for r in attempts] == list(range(1, len(attempts) + 1)) and
                all(type(r['start_ns']) is int and type(r['headers_ns']) is int and
                    0 < r['start_ns'] < r['headers_ns'] and r['method'] == 'POST' and r['path'] == cancel
                    for r in attempts) and attempts[-1]['status'] == 200, 'unresolved cancellation client')
        for previous, retry in zip(attempts, attempts[1:]):
            require(previous['status'] == 429 and previous['error'] == 'gateway_rate_limit'
                    and previous['retry_after'] == '1'
                    and retry['start_ns'] >= previous['headers_ns'] + 1_000_000_000,
                    'cancellation retry did not honor ingress admission')
        first_attempts.append(attempts[0])
    require(max(r['start_ns'] for r in first_attempts) < min(r['headers_ns'] for r in first_attempts),
            'SDK cancellation requests did not overlap')
    require(all(r['query'] == '' for r in writer['requests']), 'unexpected Batch submission query')
    counts = Counter(map(route,reader['requests']))
    for job in (complete,pending):
        require(1 <= counts.pop(('GET',job,200),0) <= 18, 'missing or excessive Batch polls')
    output = finished['output_file_id']
    require(isinstance(output,str) and output, 'published Batch output missing')
    expected = Counter({('POST', cancel, 200):1, ('POST', complete+'/cancel', 409):1,
                        ('GET', complete, 404):1, ('GET',batches,200):2,
                        ('GET',files+'/'+output+'/content',200):1, ('POST','/v1/chat/completions',200):1})
    for ident in {held['input'],done['input'],output,stopped.get('output_file_id'),stopped.get('error_file_id')}-{None}:
        expected['DELETE',files+'/'+ident,200] += 1
    require(counts == expected, 'SDK reads, conflicts or File cleanup differ')
    queries = [sorted(parse_qsl(r['query'],strict_parsing=True)) for r in reader['requests'] if r['path']==batches]
    require(queries == [[('limit','1')], [('after',pages[0]),('limit','1')]], 'wrong cursor query')
    require(all(r['query']=='' for r in reader['requests'] if r['path']!=batches), 'unexpected query')
    wires = writer['requests']+reader['requests']
    ids = {r['trace'] for r in wires}
    require(len(ids) == len(wires) <= 55 and all(isinstance(t,str) and t.startswith('req-') for t in ids),
            'duplicate, missing or excessive HTTP requests')
    return ids
