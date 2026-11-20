"""Inspect original duplex events; keep model quality separate from transport."""
import re
from tools.serve_realtime import speech_checks
from tools.serve_service.package import require

LABELS = dict(manual=['manual_private', 'mixed', 'inline_audio', 'roundtrip',
                      'tool', 'tool_answer'], vad=['vad_auto', 'idle', 'interrupt'])


def judgment(label, actual, expected):
    normalize = lambda text: re.sub(r'[\s。，,.!！?？]', '', text)
    return dict(label=label, actual=actual, expected=expected,
                status='PASS' if normalize(actual) == normalize(expected) else 'FAIL')


def text(response):
    return ''.join(p['text'] for item in response['output']
                   if item['type'] == 'message' for p in item['content']
                   if p['type'] == 'output_text')


def response_rows(value):
    mode, events, rows = value['mode'], value['events'], value['responses']
    require(mode in LABELS and [r['label'] for r in rows] == LABELS[mode],
            'duplex response coverage')
    ids = [r['response']['id'] for r in rows]
    require(len(ids) == len(set(ids)), 'replayed response identity')
    require([e['response'] for e in events if e['type'] == 'response.done'] ==
            [r['response'] for r in rows], 'actual terminals differ or missing')
    require([e['response']['id'] for e in events if e['type'] == 'response.created'] ==
            ids, 'response creation ownership')
    total = 0
    for row in rows:
        response = row['response']
        require(response['status'] == ('cancelled' if row['label'] == 'interrupt'
                                        else 'completed'), 'response state')
        total += speech_checks.usage(response)
        if response['status'] == 'completed' and response['output_modalities'] == ['text']:
            delta = ''.join(e['delta'] for e in events if
                e['type'] == 'response.output_text.delta' and
                e['response_id'] == response['id'])
            require(delta == text(response), 'stream/terminal mismatch')
    return {r['label']: r['response'] for r in rows}, total


def input_rows(events, expected_parts, phrase):
    rows = [e for e in events if e['type'] ==
            'conversation.item.input_audio_transcription.completed']
    require([(r['item_id'], r['content_index']) for r in rows] == expected_parts,
            'actual ASR part identity/order/count')
    require(not any(e['type'] == 'conversation.item.input_audio_transcription.failed'
                    for e in events), 'actual ASR failure')
    total = sum(speech_checks.usage(r) for r in rows)
    quality = [judgment('asr_' + r['item_id'] + '_' + str(r['content_index']),
                         r['transcript'], phrase) for r in rows]
    return total, quality


def temporary_cleanup(value):
    for name in ('temporary_audio', 'temporary_roundtrip'):
        expected = [e for e in value['events'] if e['type'] == 'error' and
                    e['error'].get('event_id') == 'missing_' + name]
        require(len(expected) == 1 and expected[0]['error']['code'] ==
                'realtime_item_not_found', 'temporary history retained')
        require(not any(e['type'] in ('conversation.item.added', 'conversation.item.done',
                        'conversation.item.retrieved') and e['item']['id'] == name
                        for e in value['events']), 'temporary input entered public history')
    require(len([e for e in value['events'] if e['type'] == 'error']) == 2,
            'unexpected input/response error')


def idle_boundary(value):
    events = value['events']
    idle = [e for e in events if e['type'] == 'input_audio_buffer.timeout_triggered']
    require(len(idle) == 1 and idle[0]['audio_start_ms'] == idle[0]['audio_end_ms'],
            'idle must contain zero received samples')
    require(not any(e['type'].startswith('conversation.item.input_audio_transcription.')
                    and e['item_id'] == idle[0]['item_id'] for e in events),
            'zero-input ASR success fabricated')
    commits = [e for e in events if e['type'] == 'input_audio_buffer.committed']
    require(len(commits) == 3 and commits[1]['item_id'] == idle[0]['item_id'],
            'VAD/idle ownership')
    starts = [e for e in events if e['type'] == 'input_audio_buffer.speech_started']
    require(len(starts) == 2 and starts[0]['item_id'] == commits[0]['item_id'] and
            starts[1]['item_id'] == commits[2]['item_id'], 'actual VAD boundaries')
    cancelled = value['responses'][2]['response']
    at = next(i for i,e in enumerate(events) if e['type'] == 'response.created' and
              e['response']['id'] == cancelled['id'])
    speech_at = events.index(starts[1])
    require(at < speech_at and any(e['type'] == 'response.output_text.delta' and
            e['response_id'] == cancelled['id'] and e['delta']
            for e in events[at:speech_at]), 'interrupt must follow actual model delta')
    return [(commits[i]['item_id'], 0) for i in (0, 2)]


def observe(value, phrase, record):
    mode = value['mode']
    require(value['status'] == 'PASS' and value['transport'] == 'official-openai-wss'
            and value['connections'] == 1 and value['close_code'] == 1000 and
            value['automatic_replay'] is False and value['response_creates'] ==
            (6 if mode == 'manual' else 1), 'actual SDK connection boundary')
    events = value['events']
    require(len({e['event_id'] for e in events}) == len(events), 'replayed event ID')
    responses, tokens = response_rows(value)
    quality = []
    if mode == 'manual':
        retrievals = {v['label']: v['item'] for v in value['retrievals']}
        require(len(retrievals) == len(value['retrievals']) == 3,
                'retrieval coverage')
        private = retrievals['manual_private']
        require('transcript' not in private['content'][0] and
                speech_checks.decode64(private['content'][0]['audio']) and
                not any(e['type'].startswith('conversation.item.input_audio_transcription.')
                        and e['item_id'] == private['id'] for e in events),
                'private ASR disclosure or missing waveform')
        require(len([e for e in events if e['type'] == 'input_audio_buffer.committed'])
                == 1, 'direct input duplicated buffer commit')
        mixed = retrievals['mixed']
        require(mixed['id'] == 'mixed_input' and len(mixed['content']) == 4 and
                mixed['content'][1]['transcript'] == 'REFERENCE_ONLY_NEVER_SPOKEN'
                and all(speech_checks.decode64(mixed['content'][i]['audio'])
                        for i in (1, 3)), 'mixed retrieval/public reference')
        for kind in ('conversation.item.added', 'conversation.item.done'):
            require(len([e for e in events if e['type'] == kind and
                         e['item']['id'] == 'mixed_input']) == 1,
                    'mixed parent duplicated')
        temporary_cleanup(value)
        public = [('mixed_input', 1), ('mixed_input', 3),
                  ('temporary_audio', 1), ('temporary_roundtrip', 0)]
        _, speech = speech_checks.audio(events, responses['inline_audio'],
                                         retrievals['inline_audio'])
        for label in ('manual_private', 'mixed', 'inline_audio', 'roundtrip'):
            quality.append(judgment(label, speech['transcript'] if label ==
                'inline_audio' else text(responses[label]), phrase))
        calls = responses['tool']['output']
        require(len(calls) == 1 and calls[0]['type'] == 'function_call' and
                calls[0]['name'] == 'lookup_project_record', 'actual tool call')
        import json
        require(json.loads(calls[0]['arguments']) == {'record_key': 'release'} and
                value['tool_record'] == dict(record_key='release',
                version=record['version'], source='docs/STATUS.md',
                source_sha256=record['sha256']), 'actual project tool source')
        outputs = [e['item'] for e in events if e['type'] == 'conversation.item.done'
                   and e['item']['type'] == 'function_call_output']
        require(len(outputs) == 1 and outputs[0]['call_id'] == calls[0]['call_id']
                and json.loads(outputs[0]['output']) == value['tool_record'],
                'real tool result not returned')
        quality.append(judgment('tool_answer', text(responses['tool_answer']), record['version']))
    else:
        require(not any(e['type'] == 'error' for e in events), 'VAD protocol error')
        public = idle_boundary(value)
        quality = [judgment('vad_auto', text(responses['vad_auto']), phrase),
                   judgment('idle', text(responses['idle']), 'READY')]
    require(value['quality'] == quality, 'quality must match original answers')
    asr_tokens, asr_quality = input_rows(events, public, phrase)
    return dict(visible_tokens=tokens + asr_tokens, admitted=11 if mode == 'manual' else 5,
                private_asr=1 if mode == 'manual' else 0,
                quality=quality + asr_quality)
