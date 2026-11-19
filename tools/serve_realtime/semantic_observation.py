"""Observe bounded semantic fallback without qualifying a model profile.

The strict successful-classifier evidence contract remains unchanged. This
reader retains the actual counters and distinguishes unknown completion at the
silence limit from completion using a real score. It never rewrites a receipt.
"""
import math

from tools.serve_recovery import ledger, profile_evidence as evidence

POLICY = 'record_bounded_semantic_fallback_v1'
STATUS = 'OBSERVED_NOT_QUALIFIED'


def observe(value, continuation):
    require = ledger.require
    require(type(value) is dict and value.get('status') == STATUS and
        value.get('observation_policy') == POLICY and
        value.get('transport') == 'openai-realtime-wss' and
        type(value.get('connections')) is int and value['connections'] == 1 and
        type(value.get('close_code')) is int and value['close_code'] == 1000 and
        value.get('automatic_replay') is False and
        len(ledger.canonical(value)) <= 4 << 20,
        'semantic observation lifecycle or explicit policy differs')
    terminal = value['terminal']; use = terminal['usage']
    require(terminal.get('type') ==
            'conversation.item.input_audio_transcription.completed' and
        type(terminal.get('item_id')) is str and 0 < len(terminal['item_id']) <= 128 and
        terminal.get('transcript') == evidence.ASR_TEXT and
        value.get('delta') == terminal['transcript'] and use.get('type') == 'tokens' and
        all(ledger.integer(use.get(k), minimum=1)
            for k in ('input_tokens', 'output_tokens', 'total_tokens')) and
        use['input_tokens'] + use['output_tokens'] == use['total_tokens'],
        'actual semantic transcript or usage differs')
    detail = use['input_token_details']
    require(all(ledger.integer(detail.get(k), minimum=1)
                for k in ('audio_tokens', 'text_tokens')) and
        detail['audio_tokens'] + detail['text_tokens'] == use['input_tokens'],
        'actual semantic input accounting differs')
    score = terminal['q4t_semantic_vad']
    counters = ('attempts', 'accepted', 'unavailable', 'invalid', 'stale',
                'input_tokens', 'output_tokens')
    require(score.get('profile') == 'q4t-native-semantic-turn-v1' and
        all(ledger.integer(score.get(k)) for k in counters) and
        0 < score['attempts'] <= 8 and
        sum(score[k] for k in counters[1:5]) == score['attempts'] and
        score.get('included_in_usage') is True and
        0 < score['input_tokens'] < use['input_tokens'] and
        0 < score['output_tokens'] < use['output_tokens'] and score['output_tokens'] <= 128 and
        type(score.get('probability_available')) is bool and
        type(score.get('maximum_wait')) is bool,
        'actual classifier work or bounded accounting differs')
    if score['probability_available']:
        require(score['accepted'] > 0, 'probability without accepted classifier')
        outcome = 'scored_maximum_wait' if score['maximum_wait'] else 'scored_early_end'
    else:
        require(score['maximum_wait'] is True,
                'unknown semantic probability cannot end a turn early')
        outcome = 'unknown_maximum_wait'
    logs = terminal['logprobs']
    require(type(logs) is list and 0 < len(logs) <= use['output_tokens'],
            'actual transcript logprobs missing')
    for row in logs:
        require(type(row.get('token')) is str and
            type(row.get('logprob')) in (int, float) and math.isfinite(row['logprob']) and
            row['logprob'] <= 0 and type(row.get('bytes')) is list and row['bytes'] and
            all(ledger.integer(b) and b <= 255 for b in row['bytes']),
            'invalid transcript logprob')
    require(bytes(b for row in logs for b in row['bytes']) == evidence.ASR_TEXT.encode(),
            'logprob bytes differ from the actual transcript')
    events = value['events']
    require(type(events) is list and 0 < len(events) <= 2048 and
        all(type(e) is dict for e in events) and
        not any(e.get('type') in ('error',
            'conversation.item.input_audio_transcription.failed') for e in events),
        'semantic protocol error cannot be hidden by completion')
    positions = []
    for kind in ('input_audio_buffer.speech_started', 'input_audio_buffer.speech_stopped',
                 'input_audio_buffer.committed',
                 'conversation.item.input_audio_transcription.completed'):
        matches = [(i, e) for i, e in enumerate(events) if e.get('type') == kind]
        require(len(matches) == 1 and matches[0][1].get('item_id') == terminal['item_id'],
                'semantic turn identity or terminal multiplicity differs')
        positions.append(matches[0][0])
    require(positions == sorted(positions) and events[positions[-1]] == terminal,
            'semantic terminal changed or events reordered')
    deltas = [(i, e) for i, e in enumerate(events) if
              e.get('type') == 'conversation.item.input_audio_transcription.delta']
    require(deltas and all(positions[0] < i < positions[-1] and
        e.get('item_id') == terminal['item_id'] and type(e.get('delta')) is str
        for i, e in deltas) and
        ''.join(e['delta'] for _, e in deltas) == terminal['transcript'],
        'actual transcript stream differs')
    pause = value.get('continuation')
    if continuation:
        require(type(pause) is dict and pause.get('no_commit') is True and
            pause.get('original_samples_sent') == 36000 and
            pause.get('pause_samples') == 12000 and
            pause.get('total_original_samples') == 100895,
            'unfinished prefix or actual suffix missing')
    else:
        require(pause is None, 'unexpected continuation')
    retrieved = value['retrieved']
    matches = [(i, e) for i, e in enumerate(events) if
               e.get('type') == 'conversation.item.retrieved']
    require(len(matches) == 1 and matches[0][0] > positions[-1] and
        matches[0][1] == retrieved, 'actual retained item retrieval differs')
    item = retrieved['item']; content = item['content']
    require(item['id'] == terminal['item_id'] and len(content) == 1 and
        content[0]['transcript'] == terminal['transcript'] and
        ledger.integer(value.get('sent_samples'), minimum=1),
        'retained waveform identity differs')
    waveform = evidence.semantic_pcm(content[0]['audio'])
    require(0 < len(waveform) <= value['sent_samples'] * 2,
            'retained waveform exceeds actual input')
    return dict(kind=outcome, total_tokens=use['total_tokens'], classifier=dict(score),
        retained_bytes=len(waveform), profile_qualified=False)
