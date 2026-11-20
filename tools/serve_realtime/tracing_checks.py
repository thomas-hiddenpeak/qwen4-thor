"""Independent wire/persistence checks for native auxiliary tracing."""
import hashlib
import json
import math

from tools.serve_service.package import require
from tools.serve_realtime.mcp_checks import ARGUMENTS, response_usage

TRACE_KEYS = frozenset(('object', 'observation_boundary', 'version', 'id',
    'session_id', 'conversation_id', 'workflow_name', 'group_id', 'metadata',
    'status', 'close_reason', 'started_at_ms', 'ended_at_ms', 'duration_ms',
    'revision', 'events_seen', 'dropped_span_events', 'collector_errors',
    'spans', 'persistence'))
SPAN_KEYS = frozenset(('id', 'kind', 'entity_id', 'parent_span_id',
    'parent_entity_id', 'status', 'phase', 'approval_request_id', 'partial',
    'started_at_ms', 'ended_at_ms', 'duration_ms', 'dispatched_at_ms', 'usage'))
KINDS = frozenset(('response', 'mcp_import', 'mcp_call', 'mcp_approval',
    'function_arguments', 'input_transcription', 'output_audio'))


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def safe_usage(value):
    require(type(value) is dict and set(value) <= {
        'input_tokens', 'output_tokens', 'total_tokens',
        'input_token_details', 'output_token_details'}, 'trace usage fields')
    for key, child in value.items():
        if key.endswith('_details'):
            require(type(child) is dict and set(child) <= {
                'text_tokens', 'audio_tokens', 'cached_tokens', 'image_tokens'},
                'trace usage detail fields')
            require(all(type(v) is int and 0 <= v < 2**53 for v in child.values()),
                    'numeric usage details only')
        else:
            require(type(child) is int and 0 <= child < 2**53, 'numeric usage only')


def snapshot(value, session, responses, call, approval, *, closed=False):
    """A trace must agree with observed protocol identities, not answer text."""
    require(set(value) == TRACE_KEYS and value['object'] == 'realtime.trace' and
        value['version'] == 1 and value['observation_boundary'] == 'native_protocol_owner' and
        value['session_id'] == session and value['id'] == 'trace_' +
        hashlib.sha256(session.encode()).hexdigest(), 'trace schema or owner')
    require(value['status'] == ('closed' if closed else 'in_progress') and
        value['workflow_name'] == 'q4t-project-query' and
        type(value['revision']) is int and value['revision'] > 0 and
        value['dropped_span_events'] == value['collector_errors'] == 0,
        'complete bounded trace lifecycle required')
    require(number(value['started_at_ms']) and
        (number(value['ended_at_ms']) and number(value['duration_ms']) if closed else
         value['ended_at_ms'] is value['duration_ms'] is None), 'trace duration boundary')
    persisted = value['persistence']
    require(set(persisted) == {'state', 'object_revision', 'snapshot_revision',
        'owner_interrupted', 'capture_interrupted', 'expires_at'} and
        persisted['state'] == 'durable_snapshot' and persisted['object_revision'] > 0 and
        persisted['snapshot_revision'] == value['revision'] and
        persisted['owner_interrupted'] is persisted['capture_interrupted'] is False and
        persisted['expires_at'] > 0, 'confirmed persistence and revision required')
    spans = value['spans']
    require(type(spans) is list and len(spans) <= 256 and
        len({s['id'] for s in spans}) == len(spans) and
        len({(s['kind'], s['entity_id']) for s in spans}) == len(spans),
        'unique bounded spans required')
    for span in spans:
        require(set(span) == SPAN_KEYS and span['kind'] in KINDS and
            span['status'] == 'completed' and span['partial'] is False and
            all(number(span[k]) for k in ('started_at_ms', 'ended_at_ms', 'duration_ms')),
            'complete observed span; payload fields forbidden')
        safe_usage(span['usage'])
    actual = {s['entity_id']: s for s in spans if s['kind'] == 'response'}
    require(set(actual) == {r['id'] for r in responses}, 'trace response coverage')
    for response in responses:
        expected = response['usage']
        require(all(actual[response['id']]['usage'].get(k) == expected[k] for k in
            ('input_tokens', 'output_tokens', 'total_tokens')), 'trace usage differs from wire')
    calls = [s for s in spans if s['kind'] == 'mcp_call']
    approvals = [s for s in spans if s['kind'] == 'mcp_approval']
    imports = [s for s in spans if s['kind'] == 'mcp_import']
    parent = next((r for r in responses if any(i.get('id') == call['id']
        for i in r['output'])), None)
    require(parent is not None and len(calls) == len(approvals) == len(imports) == 1 and
        calls[0]['entity_id'] == call['id'] and
        calls[0]['parent_entity_id'] == parent['id'] and
        calls[0]['parent_span_id'] == actual[parent['id']]['id'] and
        calls[0]['approval_request_id'] == approval['id'] and
        approvals[0]['entity_id'] == approval['id'] and
        number(calls[0]['dispatched_at_ms']), 'MCP trace ownership/approval/dispatch')
    require(len(spans) == len(responses) + 3, 'unexpected fabricated span')
    return len(spans)


def observe(receipt, journal, remote, source, before, after):
    require(receipt['mode'] in ('primary', 'light') and
        receipt['status'] in ('PASS', 'QUALITY_FAIL') and
        receipt['transport'] == 'official-openai-wss-and-http' and
        receipt['connections'] == 1 and receipt['close_code'] == 1000 and
        receipt['automatic_replay'] is False and receipt['events'] == journal and
        not any(e['type'] == 'error' for e in journal), 'actual SDK journal/connection')
    sessions = [e['session'] for e in journal if e['type'] in
                ('session.created', 'session.updated')]
    require(sum(e['type'] == 'session.created' for e in journal) == 1 and
        len(sessions) >= 2 and all(s.get('id') == receipt['session_id'] and
        s.get('object') == 'realtime.session' for s in sessions),
        'stable public session identity must bind the actual trace query')
    responses = receipt['responses']
    require(len(responses) == len({r['id'] for r in responses}) == 3 and
        all(r['status'] == 'completed' for r in responses) and
        [e['response'] for e in journal if e['type'] == 'response.done'] == responses,
        'three unique actual completed generations')
    tokens = sum(response_usage(r) for r in responses)
    require(after['admitted']-before['admitted'] == 3 and
        after['settled']-before['settled'] == tokens and after['free_slots'] == 1 and
        all(after[k] == 0 for k in ('reserved', 'active', 'sessions', 'host_bytes')),
        'actual trace business settlement/cleanup')
    require(len(receipt['tools']) == len(remote['calls']) == 1 and
        remote['methods'].count('tools/call') == 1 and remote['calls'][0]['ok'] and
        remote['calls'][0]['source'] == source, 'one real project read without replay')
    call = receipt['tools'][0]
    require(call['name'] == 'read_project_record' and call['server_label'] == 'project' and
        json.loads(call['arguments']) == ARGUMENTS and not call.get('error') and
        json.loads(call['output'])['structuredContent'] == remote['calls'][0],
        'actual tool receipt differs from project source')
    generated = [(r, i) for r in responses for i in r['output']
                 if i['type'] == 'mcp_call']
    require(len(generated) == 1, 'one generated MCP call')
    parent, item = generated[0]
    require(all(item.get(k) == call[k] for k in ('id', 'name', 'server_label', 'arguments')) and
        not item.get('output') and not item.get('error') and ''.join(e['delta'] for e in journal
        if e['type'] == 'response.mcp_call_arguments.delta' and
        e.get('response_id') == parent['id'] and e.get('item_id') == call['id']) == call['arguments'],
        'generated call or argument stream differs')
    terminals = [e for e in journal if e['type'] == 'response.output_item.done' and
                 e.get('item', {}).get('id') == call['id']]
    require(len(terminals) == 1 and terminals[0]['item'] == call and
        journal.index(terminals[0]) > next(i for i,e in enumerate(journal) if
        e['type'] == 'response.done' and e['response']['id'] == parent['id']),
        'actual tool terminal follows generation exactly once')
    for response in responses:
        chunks = ''.join(e['delta'] for e in journal if e['type'] == 'response.output_text.delta'
                         and e.get('response_id') == response['id'])
        output = ''.join(p['text'] for i in response['output'] if i['type'] == 'message'
                         for p in i['content'] if p['type'] == 'output_text')
        require(chunks == output, 'actual text stream differs from terminal')
    proposal = [e['item'] for e in journal if e['type'] == 'conversation.item.done' and
        e.get('item', {}).get('type') == 'mcp_approval_request']
    decisions = [e for e in journal if e['type'] == 'conversation.item.done' and
        e.get('item', {}).get('type') == 'mcp_approval_response']
    dispatches = [e for e in journal if e['type'] == 'response.mcp_call.in_progress']
    require(len(proposal) == len(decisions) == len(dispatches) == 1 and
        decisions[0]['item']['approval_request_id'] == proposal[0]['id'] and
        decisions[0]['item']['approve'] is True and dispatches[0]['item_id'] == call['id'] and
        journal.index(decisions[0]) < journal.index(dispatches[0]), 'actual approval order')
    observed_responses = responses if receipt['mode'] == 'primary' else responses[:2]
    spans = snapshot(receipt['snapshot'], receipt['session_id'], observed_responses,
                     call, proposal[0])
    require(receipt['trace_id'] == receipt['snapshot']['id'], 'trace ID differs')
    rows = {r['label']: r for r in receipt['operations']}
    for label in ('cross-scope-get', 'cross-scope-delete'):
        require(rows[label]['status'] == 404 and rows[label]['scope'] == 'other',
                'foreign scope read/deletion not rejected')
    require(rows['cross-scope-list']['scope'] == 'other' and
        rows['cross-scope-list']['body']['data'] == [], 'scope list isolation')
    require(rows['retrieve']['body']['metadata'] == {
        'case': receipt['mode'], 'opaque': 'caller-owned-private-metadata'}, 'private metadata')
    require(rows['summary']['body']['data'] and all(v.get('summary') is True and
        'spans' not in v and 'metadata' not in v for v in rows['summary']['body']['data']),
        'list must be a bounded payload-free summary')
    if receipt['mode'] == 'primary':
        require(receipt['signals'] == ['repair-storage'] and
            rows['storage-unavailable']['status'] == 503, 'actual storage outage missing')
        snapshot(receipt['final_snapshot'], receipt['session_id'], responses, call, proposal[0], closed=True)
    else:
        require(receipt['signals'] == ['kill-trace-worker'] and
            receipt['retained']['status'] == 'closed' and
            rows['delete-active']['body']['deleted'] is True and
            all(rows[k]['status'] == 404 for k in ('no-resurrection', 'closed-still-deleted')) and
            any(k.startswith('worker-recovered-') and v['status'] == 200 for k,v in rows.items()),
            'actual worker recovery, durable restart or deletion missing')
    text = lambda r: ''.join(p['text'] for i in r['output'] if i['type'] == 'message'
        for p in i['content'] if p['type'] == 'output_text').strip()
    ordinary = responses[0 if receipt['mode'] == 'primary' else 2]
    answer = responses[2 if receipt['mode'] == 'primary' else 1]
    title = source['text'].splitlines()[0].split(': ',1)[1].lstrip('# ')
    quality = [dict(case='ordinary', answer=text(ordinary), status='PASS' if text(ordinary)=='42' else 'FAIL'),
        dict(case='document', answer=text(answer), status='PASS' if title in text(answer) and
             source['citation'] in text(answer) else 'FAIL')]
    if receipt['mode'] == 'light':quality.reverse()
    require(quality == receipt['quality'] and receipt['status'] ==
        ('PASS' if all(r['status']=='PASS' for r in quality) else 'QUALITY_FAIL'),
        'original model quality must remain separate')
    return dict(protocol='PASS',quality=quality,model_jobs=3,settled_tokens=tokens,
        spans=spans,actual_reads=1,automatic_replays=0,resources_released=True)
