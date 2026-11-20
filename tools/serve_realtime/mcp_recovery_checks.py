"""Independent wire, real read, approval and settlement recovery contracts."""
import json
from tools.serve_realtime import duplex_checks, managed, mcp_checks as c
from tools.serve_service.package import require

FOLLOW_PROMPT = ('Use the actual tool result already in this conversation. Reply '
    'with its first-line document title and exact citation only. Do not invent a source.')
MODES = ('complete', 'hold_approval', 'hold_complete', 'policy', 'hold_policy')


def work(mode):
    require(mode in MODES, 'fixed MCP recovery mode')
    return 1 if mode in ('hold_approval', 'policy', 'hold_policy') else 2


def check_session(value, mode, transport, journal, source, history):
    duplex_checks.journal_matches(journal, value)
    events, responses = value['events'], value['responses']
    policy = mode in ('policy', 'hold_policy')
    complete = mode in ('complete', 'policy')
    jobs = work(mode)
    require(value.get('phase') == 'result' and value.get('status') == 'PASS' and
        value.get('mode') == mode and value.get('transport') == transport and
        transport in ('official-openai-wss', 'ws-loopback') and
        value.get('connections') == 1 and value.get('automatic_replay') is False and
        value.get('response_creates') == jobs and
        value.get('close_code') in ([1000] if complete else [1001, 1006, 1011, 1012]) and
        value.get('client_journal_authorization') == 'redacted',
        'MCP recovery transport or replay')
    require(0 < len(events) <= 4096 and 0 < value['event_bytes'] <= 4 << 20 and
        len({e['event_id'] for e in events}) == len(events) and
        not any(e['type'] == 'error' for e in events) and
        type(value['transport_errors']) is list and len(value['transport_errors']) <= 4 and
        (not complete or not value['transport_errors']), 'MCP event bounds or error')
    client = [r['event'] for r in journal if r['direction'] == 'client']
    require(len(client) == value['sent_events'] <= 32 and
        sum(e['type'] == 'session.created' for e in events) == 1,
        'client/event count changed')
    updates = [e for e in client if e['type'] == 'session.update']
    require(len(updates) == 1 and len(updates[0]['session']['tools']) == 1,
            'one explicit MCP session definition required')
    definition = updates[0]['session']['tools'][0]
    require(definition['type'] == 'mcp' and definition['server_label'] == 'project' and
        definition['allowed_tools'] == ['read_project_record'] and
        definition['require_approval'] == 'always' and
        definition['authorization'] == '<redacted>', 'MCP definition or public credentials')
    require(len(responses) == jobs and len({r['id'] for r in responses}) == jobs and
        all(r['status'] == 'completed' and r['output_modalities'] == ['text'] for r in responses) and
        [e['response'] for e in events if e['type'] == 'response.done'] == responses and
        [e['response']['id'] for e in events if e['type'] == 'response.created'] ==
            [r['id'] for r in responses], 'actual model terminal lost or repeated')
    creates = [e['response'] for e in client if e['type'] == 'response.create']
    require(len(creates) == jobs and all(r['conversation'] == 'auto' and
        'input' not in r and r['max_output_tokens'] == 256 for r in creates),
        'generation did not consume owned history')
    questions = [('user_mcp_after_fault', c.ORDINARY_PROMPT)] if policy else [
        ('user_mcp_read', c.READ_PROMPT)] + ([] if mode == 'hold_approval' else [
            ('user_mcp_follow', FOLLOW_PROMPT)])
    for response, (identifier, prompt) in zip(responses, questions):
        rows = [e for e in events if e['type'] == 'conversation.item.done' and
                e.get('item', {}).get('id') == identifier]
        created = next(e for e in events if e['type'] == 'response.created' and
                       e['response']['id'] == response['id'])
        require(len(rows) == 1 and rows[0]['item']['role'] == 'user' and
            rows[0]['item']['content'] == [dict(type='input_text', text=prompt)] and
            events.index(rows[0]) < events.index(created), 'new explicit user task missing')
        delta = ''.join(e['delta'] for e in events if
            e['type'] == 'response.output_text.delta' and e['response_id'] == response['id'])
        require(delta == duplex_checks.text(response), 'text stream differs from terminal')
    require(len(history) <= 1 and value['restored'] == history,
            'exact prior completed history required')
    for item in history:
        require(item['type'] == 'mcp_call' and item.get('output') and not item.get('error') and
            [e['item'] for e in events if e['type'] == 'conversation.item.done' and
             e.get('item', {}).get('id') == item['id']] == [item] and
            [e['item'] for e in client if e['type'] == 'conversation.item.create' and
             e.get('item', {}).get('id') == item['id']] == [item],
            'retained history was not authenticated unchanged')
    approvals = [e['item'] for e in events if e['type'] == 'conversation.item.done' and
                 e.get('item', {}).get('type') == 'mcp_approval_request']
    decisions = [e for e in events if e['type'] == 'conversation.item.done' and
                 e.get('item', {}).get('type') == 'mcp_approval_response']
    dispatch = [e for e in events if e['type'] == 'response.mcp_call.in_progress']
    catalogs = [e for e in events if e['type'] in
                ('mcp_list_tools.completed', 'mcp_list_tools.failed')]
    require(len(catalogs) == 1 and catalogs[0]['type'] ==
        ('mcp_list_tools.failed' if policy else 'mcp_list_tools.completed'),
        'actual private policy/import boundary missing')
    quality, call_ids = [], []
    if policy:
        require(not history and not approvals and not decisions and not dispatch and
            not value['tools'] and value['approval'] is None and
            all(r['tools'] == [] and r['tool_choice'] == 'none' for r in creates),
            'denied private route dispatched or blocked ordinary work')
        answer = duplex_checks.text(responses[0]).strip()
        quality.append(dict(case='ordinary_after_fault', answer=answer,
                            status='PASS' if answer == '42' else 'FAIL'))
    else:
        generated = [i for i in responses[0]['output'] if i['type'] == 'mcp_call']
        require(len(generated) == 1, 'one actual model-generated tool call')
        call = generated[0]
        call_ids.append(call['id'])
        require(not call.get('output') and not call.get('error') and
            call['name'] == 'read_project_record' and call['server_label'] == 'project' and
            json.loads(call['arguments']) == c.ARGUMENTS and len(approvals) == 1 and
            approvals[0] == value['approval'] and all(approvals[0][k] == call[k]
            for k in ('name', 'arguments', 'server_label')),
            'approval or generated arguments changed')
        require(creates[0]['tool_choice'] == dict(type='mcp', server_label='project',
            name='read_project_record'), 'forced tool selection changed')
        deltas = [e['delta'] for e in events if e['type'] == 'response.mcp_call_arguments.delta'
                  and e.get('response_id') == responses[0]['id'] and e.get('item_id') == call['id']]
        require(''.join(deltas) == call['arguments'], 'MCP argument stream differs')
        if mode == 'hold_approval':
            require(not decisions and not dispatch and not value['tools'],
                    'pending approval dispatched or was automatically approved')
        else:
            require(len(value['tools']) == len(decisions) == len(dispatch) == 1,
                    'tool dispatched more than once')
            actual = value['tools'][0]
            require(all(actual[k] == call[k] for k in ('id', 'name', 'arguments', 'server_label')) and
                actual['approval_request_id'] == approvals[0]['id'] and not actual.get('error') and
                decisions[0]['item']['approval_request_id'] == approvals[0]['id'] and
                decisions[0]['item']['approve'] is True and
                dispatch[0]['item_id'] == call['id'] and
                events.index(decisions[0]) < events.index(dispatch[0]), 'approval dispatch ownership')
            terminals = [e for e in events if e['type'] == 'response.output_item.done' and
                         e.get('item', {}).get('id') == call['id']]
            require(len(terminals) == 1 and terminals[0]['item'] == actual and
                events.index(dispatch[0]) < events.index(terminals[0]) and
                any(e['type'] == 'conversation.item.done' and e.get('item') == actual for e in events),
                'actual tool terminal missing or repeated')
            output = json.loads(actual['output'])
            require(output['isError'] is False and output['structuredContent']['ok'] is True and
                output['structuredContent']['source'] == source, 'actual document bytes changed')
            require(creates[1]['tools'] == [] and creates[1]['tool_choice'] == 'none',
                    'followup may not silently dispatch another tool')
            answer = duplex_checks.text(responses[1]).strip()
            title = source['text'].splitlines()[0].split(': ', 1)[1].lstrip('# ')
            quality.append(dict(case='document', answer=answer, status='PASS' if
                title in answer and source['citation'] in answer else 'FAIL'))
    return dict(tokens=sum(c.response_usage(r) for r in responses), jobs=jobs,
        ids=[r['id'] for r in responses] + call_ids, actual_reads=len(value['tools']),
        history_authenticated=len(history), quality=quality,
        model_quality_status='FAIL' if any(q['status'] != 'PASS' for q in quality) else 'PASS')


def accounting(before, after, observed):
    managed.state_shape(before)
    managed.state_shape(after)
    n = observed['jobs']
    require(after['admitted'] == before['admitted'] + n and
        after['total'] == before['total'] + n and
        after['success'] == before['success'] + n and
        after['errors'] == before['errors'] and after['aborted'] == before['aborted'] and
        after['settled'] - before['settled'] == observed['tokens'],
        'MCP work replay, missing terminal or inaccurate settlement')
    return observed['tokens']


def peer(before, after, observed, source):
    require(after['calls'][:len(before['calls'])] == before['calls'] and
        after['methods'][:len(before['methods'])] == before['methods'], 'remote receipt prefix changed')
    calls = after['calls'][len(before['calls']):]
    methods = after['methods'][len(before['methods']):]
    require(len(calls) == methods.count('tools/call') == observed['actual_reads'] and
        all(r['ok'] is True and r['source'] == source for r in calls),
        'actual remote tool read missing or replayed')
