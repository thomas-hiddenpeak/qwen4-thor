"""Targeted actual speech/tool and audio barge-in; never waive model failures."""
import json
from tools.serve_realtime import duplex_checks as c
from tools.serve_service.package import require

POLICY = 'task_scoped_v2'
INSTRUCTIONS = ('请逐字复述本轮用户提供的录音内容。保持原语言，'
                '完整输出一次，不翻译，不添加说明。')


def barge_in(events, response):
    """Require an active utterance, real VAD and one owned cancelled terminal."""
    require(response['status'] == 'cancelled' and
            response['output_modalities'] == ['audio'], 'speech not cancelled')
    created = [e for e in events if e['type'] == 'response.created']
    done = [e for e in events if e['type'] == 'response.done']
    starts = [e for e in events if e['type'] == 'input_audio_buffer.speech_started']
    commits = [e for e in events if e['type'] == 'input_audio_buffer.committed']
    require(len(created) == len(done) == len(starts) == len(commits) == 1 and
            created[0]['response']['id'] == response['id'] and
            done[0]['response'] == response and
            starts[0]['item_id'] == commits[0]['item_id'],
            'barge-in response/input ownership or duplicate')
    chunks = [e for e in events if e['type'] == 'response.output_audio.delta']
    output = response['output']
    require(len(output) == 1 and output[0]['type'] == 'message' and
            output[0]['content'][0]['type'] == 'output_audio' and chunks and
            all(e['response_id'] == response['id'] and
                e['item_id'] == output[0]['id'] and e['output_index'] == 0 and
                e['content_index'] == 0 and len(c.speech_checks.decode64(e['delta'])) > 0
                for e in chunks), 'missing or foreign actual speech')
    require(events.index(created[0]) < events.index(chunks[0]) <
            events.index(starts[0]) < events.index(done[0]) and
            max(i for i,e in enumerate(events) if
                e['type'] == 'response.output_audio.delta') < events.index(done[0]),
            'speech ended before interruption or emitted after terminal')
    category = response['usage']['output_token_details']
    require(category['audio_tokens'] > 0 and category['text_tokens'] > 0 and
            sum(category[k] for k in ('audio_tokens', 'text_tokens')) ==
            response['usage']['output_tokens'], 'cancelled audio work not accounted')
    return starts[0]['item_id']


def tool_result(events, responses, actual, record):
    calls = responses['tool']['output']
    require(len(calls) == 1 and calls[0]['type'] == 'function_call' and
            calls[0]['name'] == 'lookup_project_record' and
            json.loads(calls[0]['arguments']) == {'record_key': 'release'},
            'actual project tool call missing')
    expected = dict(record_key='release', version=record['version'],
                    source='docs/STATUS.md', source_sha256=record['sha256'])
    require(actual == expected, 'project record source changed')
    returned = [e['item'] for e in events if e['type'] == 'conversation.item.done'
                and e['item']['type'] == 'function_call_output']
    require(len(returned) == 1 and returned[0]['id'] == 'release_result' and
            returned[0]['call_id'] == calls[0]['call_id'] and
            json.loads(returned[0]['output']) == expected,
            'real tool output not returned to the model')
    return calls[0]['id']


def observe(value, phrase, record, journal):
    c.journal_matches(journal, value)
    mode, events = value['mode'], value['events']
    require(mode in ('speech_tool', 'barge_in') and value['status'] == 'PASS' and
            value['transport'] == 'official-openai-wss' and
            value['instruction_policy'] == POLICY and value['connections'] == 1 and
            value['close_code'] == 1000 and value['automatic_replay'] is False and
            value['response_creates'] == (4 if mode == 'speech_tool' else 1),
            'targeted actual session boundary')
    require(len(events) <= 8192 and len({e['event_id'] for e in events}) == len(events),
            'duplicate or oversized event journal')
    updates = [e['session'] for e in events if e['type'] == 'session.updated']
    require(len(updates) == 2 and all(s['instructions'] == INSTRUCTIONS for s in updates),
            'caller task policy differs')
    responses, tokens = c.response_rows(value)
    creates = [r['event']['response'] for r in journal if r['direction'] == 'client'
               and r['event']['type'] == 'response.create']
    if mode == 'speech_tool':
        retrieved = value['retrievals']
        require(len(retrieved) == 1 and retrieved[0]['label'] == 'inline_audio',
                'actual speech retrieval missing')
        raw, audio = c.speech_checks.audio(events, responses['inline_audio'],
                                          retrieved[0]['item'])
        c.temporary_cleanup(value)
        require(creates[0]['instructions'] == creates[1]['instructions'] == INSTRUCTIONS and
                creates[0]['input'][0]['content'][0] ==
                dict(type='input_text', text='请复述随附录音。') and
                c.speech_checks.decode64(creates[1]['input'][0]['content'][0]['audio']) == raw,
                'actual generated speech was not used for the round trip')
        call_id = tool_result(events, responses, value['tool_record'], record)
        require(creates[3]['conversation'] == 'none' and creates[3]['input'] ==
                [dict(type='item_reference', id=v) for v in
                 ('query_release', call_id, 'release_result', 'query_version')],
                'tool continuation did not use the actual conversation references')
        quality = [c.judgment('inline_audio', audio['transcript'], phrase),
                   c.judgment('roundtrip', c.text(responses['roundtrip']), phrase),
                   c.judgment('tool_answer', c.text(responses['tool_answer']), record['version'])]
        input_tokens, asr = c.input_rows(events,
            [('temporary_audio', 1), ('temporary_roundtrip', 0)], phrase)
    else:
        require(not any(e['type'] == 'error' for e in events), 'barge-in protocol error')
        setting = updates[-1]['audio']
        require(setting['output']['speed'] == .25 and
                setting['input']['turn_detection']['create_response'] is False and
                setting['input']['turn_detection']['interrupt_response'] is True and
                setting['input']['turn_detection']['idle_timeout_ms'] is None,
                'actual barge-in settings changed')
        item = barge_in(events, responses['interrupt'])
        first_chunk = next(i for i,r in enumerate(journal) if
            r['direction'] == 'server' and
            r['event']['type'] == 'response.output_audio.delta')
        appends = [i for i,r in enumerate(journal) if r['direction'] == 'client' and
                   r['event']['type'] == 'input_audio_buffer.append']
        require(appends and first_chunk < appends[0] and not any(
            r['direction'] == 'client' and r['event']['type'] == 'response.cancel'
            for r in journal), 'barge-in must follow actual audio without explicit cancel')
        input_tokens, asr = c.input_rows(events, [(item, 0)], phrase)
        quality = [c.judgment('barge_in_asr', asr[0]['actual'], phrase)]
    require(value['quality'] == quality, 'raw model quality differs from receipt')
    return dict(admitted=6 if mode == 'speech_tool' else 2, private_asr=0,
                visible_tokens=tokens + input_tokens, quality=quality + asr)
