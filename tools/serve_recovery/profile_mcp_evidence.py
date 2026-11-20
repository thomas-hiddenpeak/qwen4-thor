"""Fixed actual MCP tasks, including unmodified original model failures."""
import json
from tools.serve_recovery import ledger, profile_mcp_codec as codec
from tools.serve_recovery.profile_mcp_source import REVIEW, OBSERVED
from tools.serve_realtime import mcp_checks as checks

FORMAT = 'q4t-profile-realtime-mcp-v1'
CAPABILITY = dict(profile='q4t-realtime-mcp-v1', enabled=True, transport='native',
    credentials='private_connection_snapshot', approval=True,
    cancel_after_generation=True, automatic_model_continuation=False,
    automatic_tool_replay=False, maximum_servers=64, maximum_call_workers=1,
    maximum_call_retained_bytes=67108864)


def original(witness, name, source):
    ledger.keys(witness, ('session', 'remote', 'before', 'after'))
    value, remote = witness['session'], witness['remote']
    primary = name == 'primary'
    ledger.require(value['status'] == 'FAIL' and value['error'] == (
        'response status: incomplete' if primary else 'one actual generated MCP call') and
        value['mode'] == ('direct-deny-import' if primary else 'approve-unknown') and
        value['transport'] == 'official-openai-wss' and value['connections'] == 1 and
        value['automatic_replay'] is False, 'original MCP failure was changed')
    rows, events, tools = value['responses'], value['events'], value['tools']
    ledger.require(len(rows) == (4 if primary else 3) and
        len(tools) == (2 if primary else 1) and
        len({r['id'] for r in rows}) == len(rows) and
        [e['response'] for e in events if e['type'] == 'response.done'] == rows and
        not any(e['type'] == 'error' for e in events), 'original MCP terminals')
    tokens = sum(checks.response_usage(r) for r in rows)
    ledger.require(all(r['status'] == 'completed' for r in rows[:-1]),
                   'completed original tasks missing')
    if primary:
        ledger.require(rows[-1]['status'] == 'incomplete' and
            rows[-1]['status_details']['reason'] == 'max_output_tokens' and
            rows[-1]['usage']['output_tokens'] == 32 and
            any(e['type'] == 'mcp_list_tools.failed' for e in events) and
            witness['before'] is None and witness['after'] is None,
            'original truncation or missing-metrics limit was concealed')
    else:
        ledger.require(rows[-1]['status'] == 'completed' and
            rows[-1]['output'] and all(i['type'] == 'message' for i in rows[-1]['output']),
            'original refusal was recast as a tool call')
        before, after = witness['before'], witness['after']
        ledger.require(after['admitted'] - before['admitted'] == 3 and
            after['settled'] - before['settled'] == tokens and
            all(after[k] == 0 for k in ('reserved', 'active', 'sessions', 'host_bytes')) and
            after['free_slots'] == 1, 'original light failure settlement')
    reads = remote['calls']
    ledger.require(len(reads) == remote['methods'].count('tools/call') == 1 and
        reads[0]['ok'] and reads[0]['source'] == source,
        'original actual read or no-replay evidence changed')
    for index, actual in enumerate(tools):
        row = rows[index * 2]
        generated = [i for i in row['output'] if i['type'] == 'mcp_call']
        ledger.require(len(generated) == 1, 'one actual generated original call')
        call = generated[0]
        ledger.require(not call.get('output') and not call.get('error'),
                       'model completion fabricated a remote result')
        for item in (call, actual):
            ledger.require(item['id'] == call['id'] and item['name'] == 'read_project_record' and
                item['server_label'] == 'project' and
                json.loads(item['arguments']) == checks.ARGUMENTS, 'original tool identity')
        terminal = [e for e in events if e['type'] == 'response.output_item.done' and
                    e.get('item', {}).get('id') == actual['id']]
        done = next(e for e in events if e['type'] == 'response.done' and e['response']['id'] == row['id'])
        delta = ''.join(e['delta'] for e in events if
            e['type'] == 'response.mcp_call_arguments.delta' and e.get('item_id') == actual['id'])
        ledger.require(len(terminal) == 1 and terminal[0]['item'] == actual and
            events.index(done) < events.index(terminal[0]) and delta == call['arguments'] and
            any(e['type'] == 'conversation.item.done' and e.get('item') == actual for e in events),
            'original streamed call or retained result differs')
    result = json.loads(tools[0]['output'])
    ledger.require(result['structuredContent'] == reads[0] and result['isError'] is False and
        not tools[0].get('error'), 'original real document result missing')
    approved = tools[1 if primary else 0]
    proposals = [e['item'] for e in events if e['type'] == 'conversation.item.done' and
                 e.get('item', {}).get('type') == 'mcp_approval_request']
    decisions = [e['item'] for e in events if e['type'] == 'conversation.item.done' and
                 e.get('item', {}).get('type') == 'mcp_approval_response']
    ledger.require(len(proposals) == len(decisions) == 1 and
        approved['approval_request_id'] == proposals[0]['id'] == decisions[0]['approval_request_id'] and
        decisions[0]['approve'] is (not primary) and
        all(proposals[0][k] == approved[k] for k in ('name', 'server_label', 'arguments')),
        'original approval ownership differs')
    dispatched = [e for e in events if e['type'] == 'response.mcp_call.in_progress' and
                  e.get('item_id') == approved['id']]
    if primary:
        ledger.require(not dispatched and not approved.get('output') and
            'mcp_approval_denied' in json.dumps(approved['error']), 'denied call dispatched')
    else:
        decision = next(e for e in events if e['type'] == 'conversation.item.done' and
                        e.get('item') == decisions[0])
        ledger.require(len(dispatched) == 1 and events.index(decision) < events.index(dispatched[0]),
                       'original tool sent before approval')
    answer = ''.join(p['text'] for i in rows[1]['output'] if i['type'] == 'message'
                     for p in i['content'] if p['type'] == 'output_text').strip()
    title = source['text'].splitlines()[0].split(': ', 1)[1].lstrip('# ')
    ledger.require(title in answer and source['citation'] in answer and
        value['quality'] == [dict(case='document', answer=answer, status='PASS')],
        'original document answer differs from real data')


def business(value):
    ledger.keys(value, ('format', 'profile', 'source_review_sha256', 'witness'))
    name = value['profile']
    ledger.require(value['format'] == FORMAT and name in OBSERVED and
        value['source_review_sha256'] == ledger.digest(REVIEW), 'unknown MCP source review')
    data = codec.unpack(value['witness'])
    ledger.require(ledger.digest(data) == OBSERVED[name]['data_digest'],
                   'original or repair MCP evidence changed')
    ledger.keys(data, ('source', 'original', 'repair'))
    original(data['original'], name, data['source'])
    repair = data['repair']
    ledger.keys(repair, ('session', 'remote', 'before', 'after'))
    args = (repair['session'], repair['session']['events'], repair['remote'])
    if name == 'primary':
        result = checks.observe_repair(*args, data['original']['session']['tools'],
                                        repair['before'], repair['after'])
    else:
        result = checks.observe_unknown(*args, data['source'], repair['before'], repair['after'])
    ledger.require(all(q['status'] == 'PASS' for q in result['quality']),
                   'clarified actual task failed')
    return value
