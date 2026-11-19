"""Recovery-only observations; the strict production profile gate is unchanged.

Both successful and incorrect model answers retain exact stream/terminal/tool
provenance and usage checks. This verifier cannot qualify a primary/light
profile and never edits the observed answer to make another verifier pass.
"""
import json
import re

from tools.serve_recovery import ledger


def observe(source, session, pending_response_id=None):
    """Verify actual protocol and separately report model answer correctness.

    A managed fault may separately bind one known pending response. Its unknown
    usage, transport, lifetime and settlement are never inferred here.
    """
    ledger.keys(source, ('path', 'sha256', 'version'))
    ledger.require(source['path'] == 'docs/STATUS.md' and
        type(source['sha256']) is str and ledger.HASH.fullmatch(source['sha256']) and
        type(source['version']) is str and re.fullmatch(r'rev[0-9]+', source['version']),
        'actual project record identity missing')
    record = dict(record_key='release', version=source['version'],
                  source=source['path'], source_sha256=source['sha256'])
    ledger.require(session.get('tool_record') == record,
                   'actual read-only tool source differs')
    rows, events = session['responses'], session['events']
    ledger.require(type(rows) is list and len(rows) == 2 and
        all(type(row) is dict for row in rows) and
        type(events) is list and 0 < len(events) <= 4096 and
        all(type(e) is dict for e in events) and
        len(ledger.canonical(session)) <= 4 << 20 and
        not any(e.get('type') == 'error' for e in events),
        'conversation output bounds or errors')
    identifiers = [row['id'] for row in rows]
    ledger.require(all(type(i) is str and i for i in identifiers) and
        len(set(identifiers)) == 2 and
        [e['response'] for e in events if e.get('type') == 'response.done'] == rows and
        [e['response']['id'] for e in events if e.get('type') == 'response.created'] ==
            identifiers + ([pending_response_id] if pending_response_id else []),
        'conversation model terminal omitted or replayed')
    ledger.require(pending_response_id is None or
        (type(pending_response_id) is str and pending_response_id and
         pending_response_id not in identifiers), 'invalid pending response identity')
    total = 0
    for row in rows:
        ledger.require(row['status'] == 'completed' and
            type(row.get('output')) is list and len(row['output']) == 1 and
            row.get('output_modalities') == ['text'],
            'incomplete conversation model response')
        usage = row['usage']
        ledger.require(all(type(usage[k]) is int and usage[k] > 0 for k in
            ('input_tokens', 'output_tokens', 'total_tokens')) and
            usage['total_tokens'] == usage['input_tokens'] + usage['output_tokens'],
            'conversation actual model usage differs')
        total += usage['total_tokens']
    call, answer = rows[0]['output'][0], rows[1]['output'][0]
    ledger.require(all(type(item.get('id')) is str and item['id']
                       for item in (call, answer)) and call['id'] != answer['id'],
                   'conversation output item identity missing or repeated')
    ledger.require(call.get('type') == 'function_call' and
        call.get('status') == 'completed' and
        call.get('name') == 'lookup_project_record' and
        type(call.get('call_id')) is str and call['call_id'] and
        json.loads(call['arguments']) == dict(record_key='release'),
        'actual complete allowlisted function missing')
    ledger.require(answer.get('type') == 'message' and
        answer.get('role') == 'assistant' and answer.get('status') == 'completed' and
        type(answer.get('content')) is list and len(answer['content']) == 1 and
        answer['content'][0].get('type') == 'output_text' and
        type(answer['content'][0].get('text')) is str and
        bool(answer['content'][0]['text']) and
        rows[0].get('conversation_id') == rows[1].get('conversation_id') and
        type(rows[0].get('conversation_id')) is str and rows[0]['conversation_id'],
        'actual answer shape or shared context differs')
    for row, kind, item, expected in (
        (rows[0], 'response.function_call_arguments.delta', call, call['arguments']),
        (rows[1], 'response.output_text.delta', answer, answer['content'][0]['text'])):
        deltas = [e for e in events if e.get('type') == kind and
                  e.get('response_id') == row['id']]
        ledger.require(deltas and all(e.get('item_id') == item['id'] for e in deltas) and
            ''.join(e['delta'] for e in deltas) == expected,
            'conversation actual stream differs from terminal')
        created = next(i for i,e in enumerate(events) if
            e.get('type') == 'response.created' and e['response']['id'] == row['id'])
        done = next(i for i,e in enumerate(events) if
            e.get('type') == 'response.done' and e['response']['id'] == row['id'])
        ledger.require(all(created < i < done for i,e in enumerate(events)
                           if e in deltas), 'conversation stream event order differs')
    returned = [e['item'] for e in events if
        e.get('type') == 'conversation.item.done' and
        e.get('item', {}).get('type') == 'function_call_output']
    ledger.require(len(returned) == 1 and
        returned[0].get('call_id') == call['call_id'] and
        json.loads(returned[0]['output']) == record,
        'actual client tool return missing or unrelated')
    first_done = next(i for i,e in enumerate(events) if
        e.get('type') == 'response.done' and e['response']['id'] == identifiers[0])
    returned_at = next(i for i,e in enumerate(events) if
        e.get('type') == 'conversation.item.done' and e.get('item') == returned[0])
    second_created = next(i for i,e in enumerate(events) if
        e.get('type') == 'response.created' and e['response']['id'] == identifiers[1])
    ledger.require(first_done < returned_at < second_created,
                   'tool result did not follow actual completed call')
    actual = answer['content'][0]['text']
    return dict(total_tokens=total, model_quality=dict(
        status='PASS' if actual == source['version'] else 'FAIL',
        expected=source['version'], actual=actual,
        criterion='exact_source_version', response_id=rows[1]['id']))

