"""Actual owned native speech in both capacities, including caller isolation."""
from tools.serve_recovery import ledger, profile_evidence, profile_refusal_reuse
from tools.serve_realtime import speech_checks

DECLARATION = dict(profile='q4t-realtime-speech-v1',
    backend='native_tts_same_chat_owner', default_voice='vivian',
    formats=['audio/pcm', 'audio/pcmu', 'audio/pcma'], speed_min=.25,
    speed_max=1.5, rpm='once_per_response', voice_lock='first_audio',
    retrieve=True, truncate=True, audio_history_bytes=134217728, silent_eviction=False)
FORMAT = 'q4t-profile-realtime-speech-v1'
VOICE = profile_refusal_reuse.VOICE_ID
CASES = ('builtin', 'custom', 'foreign')
PHRASES = dict(builtin=speech_checks.PHRASE, custom='Please close the window.')
# Complete 24 kHz PCM from already accepted, unprompted-ASR-qualified WAVs.
# Exact equality is content reuse, not a claim of a new ASR or listening test.
PCM = dict(builtin=(111360, 'd2dab90e2e736165206e7aeed5702154f5c08311f01326bc9e969422f21878d5'),
    custom=(115200, 'c253466fb4d8660c01e2f32f1a18ca6276e11a9f1b2c57bbd4bc29ae573f8ebd'))


def task(mode):
    return 'Repeat exactly without adding words: ' + PHRASES.get(mode, PHRASES['custom'])


def session(value, mode):
    ledger.require(mode in CASES and value.get('status') == 'PASS' and
        value.get('mode') == 'profile_' + mode and value.get('transport') == 'official-openai-wss' and
        value.get('connections') == 1 and value.get('close_code') == 1000 and
        value.get('automatic_replay') is False and
        value.get('response_creates') == (2 if mode == 'foreign' else 1),
        'actual speech SDK boundary differs')
    labels = ['profile_foreign', 'profile_after_foreign'] if mode == 'foreign' else ['profile_' + mode]
    rows = value['responses']; events = value['events']
    ledger.require([r['label'] for r in rows] == labels and 0 < len(events) <= 4096 and
        len(ledger.canonical(value)) <= 2 << 20 and
        len({e['event_id'] for e in events}) == len(events) and
        not any(e['type'] == 'error' for e in events), 'missing or replayed speech events')
    responses = [r['response'] for r in rows]
    ledger.require([e['response'] for e in events if e['type'] == 'response.done'] == responses and
        [e['response']['id'] for e in events if e['type'] == 'response.created'] ==
        [r['id'] for r in responses] and
        len({r['id'] for r in responses}) == len(responses), 'response lifecycle differs')
    settings = [e['session'] for e in events if e['type'] == 'session.updated']
    voice = 'ryan' if mode == 'builtin' else dict(id=VOICE)
    ledger.require(settings and settings[-1]['audio']['output']['voice'] == voice and
        settings[-1]['audio']['output']['speed'] == 1, 'effective voice or speed differs')
    users = [e for e in events if e['type'] == 'conversation.item.done' and
             e.get('item', {}).get('id') == 'user_profile']
    ledger.require(len(users) == 1 and users[0]['item']['role'] == 'user' and
        users[0]['item']['content'] == [dict(type='input_text', text=task(mode))] and
        events.index(users[0]) < next(i for i,e in enumerate(events)
                                   if e['type'] == 'response.created'),
        'explicit current user task missing')
    total = 0
    if mode == 'foreign':
        failed, healthy = responses
        ledger.require(failed['status'] == 'failed' and failed['output'] == [] and
            failed['status_details']['error']['code'] == 'object_not_found' and
            not value['retrievals'] and not any(e['type'].startswith('response.output_audio.')
                for e in events), 'foreign voice must fail without audio leakage')
        # Same-owner voice resolution precedes the ordinary Chat rate lease.
        # Invalid references consume no RPM/TPM; the following healthy response
        # owns one admission. A conversation terminal is not a rate receipt.
        usage = failed['usage']
        ledger.require(all(type(usage[k]) is int and usage[k] == 0 for k in
            ('input_tokens', 'output_tokens', 'total_tokens')), 'foreign voice consumed hidden model tokens')
        total = speech_checks.usage(healthy)
        ledger.require(healthy['status'] == 'completed' and healthy['output_modalities'] == ['text'] and
            len(healthy['output']) == 1 and
            healthy['output'][0]['content'] == [dict(type='output_text', text='42')],
            'healthy text did not continue after voice isolation')
        deltas = [e for e in events if e['type'] == 'response.output_text.delta' and
                  e['response_id'] == healthy['id']]
        ledger.require(deltas and ''.join(e['delta'] for e in deltas) == '42',
                       'post-failure actual stream differs')
        expected_quality = [('profile_after_foreign', '42')]
    else:
        response = responses[0]
        ledger.require(len(value['retrievals']) == 1 and
            value['retrievals'][0]['label'] == labels[0], 'speech retrieval ownership')
        _, audio = speech_checks.audio(events, response, value['retrievals'][0]['item'])
        # The response schema makes voice optional. This engine reports the
        # effective settings in session.updated; complete PCM identity also
        # binds the actual speaker. An optional echo must not contradict it.
        output = response['audio']['output']
        ledger.require(output.get('voice', voice) == voice and
            output.get('speed', 1) == 1 and
            (audio['bytes'], audio['sha256']) == PCM[mode] and
            audio['format'] == 'audio/pcm' and audio['transcript'] == PHRASES[mode],
            'complete speech differs from independently recognized content')
        if mode == 'custom':
            # Realtime exposes aggregate preparation/synthesis input; it does
            # not yet split the ICL reference into an input audio category.
            ledger.require(response['usage']['input_tokens'] > 918,
                           'aggregate input cannot omit the 918-token ICL condition')
        total = response['usage']['total_tokens']
        expected_quality = [(labels[0], PHRASES[mode])]
    ledger.require(value['quality'] == [dict(label=label, actual=text, expected=text, status='PASS')
        for label, text in expected_quality], 'speech quality or preserved failure changed')
    return total


def business(value):
    ledger.keys(value, ('format', 'snapshot_sha256', 'sessions'))
    ledger.require(value['format'] == FORMAT and
        value['snapshot_sha256'] == profile_refusal_reuse.VOICE_SNAPSHOT,
        'speech profile source identity differs')
    ledger.keys(value['sessions'], CASES)
    for mode in CASES:
        witness = value['sessions'][mode]
        ledger.keys(witness, ('session', 'before', 'after'))
        total = session(witness['session'], mode)
        for side in ('before', 'after'):
            state = witness[side]
            ledger.keys(state, ('admitted', 'settled', 'reserved', 'active',
                                'sessions', 'host_bytes', 'free_slots'))
            ledger.require(all(ledger.integer(v) for v in state.values()) and
                all(state[k] == 0 for k in ('reserved', 'active', 'sessions', 'host_bytes')) and
                state['free_slots'] == 1, 'owned resources not settled')
        before, after = witness['before'], witness['after']
        ledger.require(after['admitted'] - before['admitted'] == 1 and
            after['settled'] - before['settled'] == total,
            'speech must own exactly one RPM and actual TPM per response')
    return value
