"""Strict Prompt qualification; old Chat evidence cannot assert this capability."""
import hashlib

from tools.serve_recovery import ledger

DECLARATION = dict(profile='q4t-realtime-prompt-v1', registry='/v1/prompts',
    definition_api='realtime', scope='authenticated_client', snapshot='immutable_version',
    explicit_fields_override=True, per_response_override=True, null_clears_response=True,
    hidden_prefix=True, input_types=['text', 'input_file', 'input_image'],
    media_admission='model_profile', file_preparation='native', rpm='once_per_response',
    usage='includes_preparation', maximum_retained_bytes=64 << 20,
    maximum_expansion_bytes=16 << 20)
CASES = ['qualified-pinned-first', 'qualified-one-response-second',
         'qualified-pinned-after-delete', 'qualified-actual-csv']


def source(value):
    ledger.keys(value, ('csv', 'sha256', 'sum', 'receipts'))
    ledger.require(type(value['csv']) is str and len(value['csv'].encode()) <= 4096 and
        hashlib.sha256(value['csv'].encode()).hexdigest() == value['sha256'] and
        ledger.integer(value['sum']) and value['sum'] > 0 and
        type(value['receipts']) is list and len(value['receipts']) == 2,
        'Prompt actual CSV source missing')
    rows = value['receipts']
    for row, label in zip(rows, ('files', 'runtime')):
        ledger.keys(row, ('label', 'path', 'sha256', 'tests'))
        ledger.require(row['label'] == label and row['path'] ==
            'docs/evidence/serve-realtime-prompt-'+label+'-cpu-20261120/acceptance.json' and
            type(row['sha256']) is str and ledger.HASH.fullmatch(row['sha256']) and
            ledger.integer(row['tests']) and row['tests'] > 0,
            'Prompt original acceptance receipt binding differs')
    ledger.require(value['sum'] == sum(row['tests'] for row in rows) and
        value['csv'] == 'stage,tests\n' + ''.join(f"{r['label']},{r['tests']}\n" for r in rows),
        'Prompt actual CSV is not derived from its sources')


def business(value):
    ledger.keys(value, ('source', 'session', 'before', 'after'))
    source(value['source'])
    session = value['session']
    ledger.require(type(session) is dict and len(ledger.canonical(session)) <= 4 << 20 and
        session.get('status') == 'PASS' and session.get('mode') == 'qualification' and
        session.get('transport') == 'official-openai-wss' and
        type(session.get('connections')) is int and session['connections'] == 1 and
        session.get('close_code') == 1000 and session.get('automatic_replay') is False and
        type(session.get('response_creates')) is int and session['response_creates'] == 4 and
        type(session.get('http_calls')) is int and session['http_calls'] == 4,
        'actual Prompt qualification SDK boundary differs')
    rows, events = session['responses'], session['events']
    ledger.require(type(rows) is list and len(rows) == 4 and
        type(events) is list and 0 < len(events) <= 4096 and
        not any(e.get('type') == 'error' for e in events) and
        [r['metadata']['acceptance_case'] for r in rows] == CASES,
        'actual Prompt qualification cases incomplete')
    ids = [r['id'] for r in rows]
    ledger.require(all(type(i) is str and i for i in ids) and len(set(ids)) == 4 and
        [e['response']['id'] for e in events if e.get('type') == 'response.created'] == ids and
        [e['response'] for e in events if e.get('type') == 'response.done'] == rows,
        'Prompt terminal missing, changed or replayed')
    total = 0
    for row, expected in zip(rows, ('42', '377', '42', str(value['source']['sum']))):
        ledger.require(row.get('status') == 'completed' and not row.get('conversation_id') and
            row.get('output_modalities') == ['text'] and len(row['output']) == 1,
            'Prompt model work or private history differs')
        item = row['output'][0]
        ledger.require(item.get('type') == 'message' and item.get('status') == 'completed' and
            item.get('role') == 'assistant' and type(item.get('id')) is str and item['id'] and
            len(item['content']) == 1 and item['content'][0].get('type') == 'output_text' and
            item['content'][0].get('text') == expected, 'Prompt actual model quality differs')
        deltas = [e for e in events if e.get('type') == 'response.output_text.delta' and
                  e.get('response_id') == row['id']]
        ledger.require(deltas and all(e.get('item_id') == item['id'] for e in deltas) and
            ''.join(e['delta'] for e in deltas) == expected, 'Prompt actual stream differs')
        start = next(i for i, e in enumerate(events) if e.get('type') == 'response.created' and
                     e['response']['id'] == row['id'])
        end = next(i for i, e in enumerate(events) if e.get('type') == 'response.done' and
                   e['response']['id'] == row['id'])
        ledger.require(all(start < i < end for i, e in enumerate(events) if e in deltas),
                       'Prompt actual stream event order differs')
        u = row['usage']
        ledger.require(all(ledger.integer(u[k]) and u[k] > 0 for k in
            ('input_tokens', 'output_tokens', 'total_tokens')) and
            u['total_tokens'] == u['input_tokens'] + u['output_tokens'],
            'Prompt actual preparation/model usage differs')
        total += u['total_tokens']
    updates = [e['session'] for e in events if e.get('type') == 'session.updated']
    identifier = session['owned']['prompt']
    ledger.require(type(identifier) is str and identifier.startswith('prompt_') and
        len(updates) == 2 and all(s['prompt']['id'] == identifier and
            s['prompt']['version'] == '1' and s['max_output_tokens'] == 128 for s in updates),
        'Prompt pinned session changed after default update/deletion')
    operations = session['operations']
    ledger.require(len(operations) == 4 and all(o['status'] == 200 for o in operations) and
        [(o['label'], o['method'], o['route']) for o in operations] == [
            ('register-qualified-arithmetic', 'POST', '/v1/prompts'),
            ('append-qualified-second', 'POST', '/v1/prompts/'+identifier+'/versions'),
            ('delete-qualified-arithmetic', 'DELETE', '/v1/prompts/'+identifier),
            ('register-qualified-file', 'POST', '/v1/prompts')] and
        operations[0]['body']['id'] == identifier and
        operations[1]['body']['default_version'] == '2' and
        operations[2]['body']['deleted'] is True, 'actual Prompt registry operations differ')
    for side in ('before', 'after'):
        state = value[side]
        ledger.keys(state, ('admitted', 'settled', 'reserved', 'active', 'sessions',
                            'host_bytes', 'free_slots'))
        ledger.require(all(ledger.integer(v) for v in state.values()) and
            all(state[k] == 0 for k in ('reserved', 'active', 'sessions', 'host_bytes')) and
            state['free_slots'] == 1, 'Prompt resources not released')
    ledger.require(value['after']['admitted'] - value['before']['admitted'] == 4 and
        value['after']['settled'] - value['before']['settled'] == total,
        'Prompt preparation and generation must settle one RPM and all actual tokens')
    return total
