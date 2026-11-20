"""Wire and accounting acceptance for a real Live client delegation turn."""
import base64
import hashlib
import math
import re
from tools.serve_service.package import require


def observe(receipt, journal, before, after, record):
    events = [row['event'] for row in journal if row['direction'] == 'server']
    require(events == receipt['events'] and receipt['status'] == 'PASS' and
            receipt['connections'] == 1 and receipt['close_code'] == 1000 and
            receipt['automatic_replay'] is False, 'complete single SDK connection')
    require(events and events[0]['type'] == 'session.started' and
            events[-1]['type'] == 'session.closed', 'actual lifecycle order')
    require(not any(e['type'] == 'error' for e in events), 'Live protocol error')
    ids = [e['event_id'] for e in events if e['type'] != 'session.output_audio.delta']
    require(len(set(ids)) == len(ids) and all(ids), 'unique owner event IDs')

    def one(kind):
        found = [e for e in events if e['type'] == kind]
        require(len(found) == 1, 'exactly one ' + kind)
        return found[0]

    started, closed = one('session.started'), one('session.closed')
    require(started['session']['id'] == closed['session']['id'] and
            started['session']['status'] == 'active' and
            closed['session']['status'] == 'active' and
            closed['reason'] == 'close_requested' and
            closed['client_event_id'] == 'finish', 'same session actual close')
    require(started['session']['audio']['format'] == {'type': 'audio/pcm', 'rate': 24000}
            and started['session']['store'] is False, 'selected actual media')
    require(one('session.input_audio.muted')['client_event_id'] == 'mute' and
            one('session.input_audio.unmuted')['client_event_id'] == 'unmute',
            'mute/unmute confirmation correlation')
    update = one('session.updated')
    require(update['client_event_id'] == 'delay' and
            update['session']['delegation']['acknowledgement_delay_ms'] == 600000,
            'effective acknowledgement update')
    # The frozen SDK retains an active resource snapshot in session.closed.
    # Actual closure is the final event/close handshake, not an invented status.
    sources = [e for e in events if e['type'] == 'session.input_transcript.delta']
    require(sources and all(e['delta'] for e in sources), 'actual transcript deltas')
    require(len({(e['start_ms'],e['end_ms']) for e in sources}) == 1,
            'one real ASR input interval')
    delegation = one('session.delegation.created')
    target = delegation['delegation']
    require(set(target) == {'type', 'id', 'target'} and
            target['type'] == 'delegation' and target['target'] == 'client' and
            target['id'] == receipt['delegation_id'], 'actual metadata-only delegation')
    thinking, commentary = one('session.thinking.appended'), one('session.commentary.appended')
    require(thinking['client_event_id'] == 'project_result' and
            commentary['client_event_id'] == 'project_answer', 'context acknowledgement ownership')
    require(events.index(sources[-1]) < events.index(delegation) < events.index(thinking)
            < events.index(commentary), 'input/handoff/context ordering')
    require(receipt['record'] == record, 'actual project retrieval source identity')
    sent = [r['event'] for r in journal if r['direction'] == 'client']
    progress = [e for e in sent if e['type'] == 'session.thinking.append']
    require(len(progress) == 1 and progress[0]['delegation_id'] == target['id'] and
            record['version'] in progress[0]['content'] and
            record['sha256'] in progress[0]['content'], 'real result actually fed back')
    transcripts = [e for e in events if e['type'] == 'session.output_transcript.delta']
    answers = [e for e in transcripts if e.get('client_event_id') == 'project_answer']
    require(len(answers) == 1 and 1 <= len(transcripts) <= 2,
            'one actual spoken result, optional same-generation preface')
    spoken = answers[0]
    pending = bytearray(); raw = bytearray(); segments = []
    for event in events:
        if event['type'] == 'session.output_audio.delta':
            require(set(event) == {'type','delta'},
                    'audio deltas have no synthetic terminal or event metadata')
            data = base64.b64decode(event['delta'], validate=True)
            pending.extend(data);raw.extend(data)
            require(len(raw) <= 2 << 20, 'bounded actual PCM output')
        elif event['type'] == 'session.output_transcript.delta':
            require(pending and len(pending)%2 == 0 and any(pending),
                    'actual nonempty speech segment')
            require(abs(event['end_ms']-event['start_ms']-len(pending)/48) <= 1,
                    'produced waveform agrees with actual transcript interval')
            segments.append(dict(transcript=event['delta'],bytes=len(pending),
                sha256=hashlib.sha256(pending).hexdigest(),
                client_event_id=event.get('client_event_id')))
            pending.clear()
    require(not pending and raw, 'no unfinalized audio after normal close')
    answer_at=events.index(spoken);context_at=events.index(commentary)
    require(context_at < answer_at < events.index(closed) and
            any(e['type']=='session.output_audio.delta' for e in events[context_at:answer_at]),
            'actual answer audio follows its application context')
    for item in (*sources, *transcripts, thinking, commentary):
        require(all(type(item[k]) is int and item[k] >= 0 for k in ('start_ms', 'end_ms'))
                and item['end_ms'] >= item['start_ms'], 'native timeline interval')
    seconds = [e['usage']['seconds'] for e in events if e['type'] in
               ('session.usage.updated', 'session.closed')]
    require(seconds and all(type(v) in (int,float) and math.isfinite(v) and v >= 0
                            for v in seconds) and seconds == sorted(seconds),
            'cumulative activity snapshots, never sum')
    key = lambda n: n+'{client="alpha"}'
    admitted = after[key('q4t_client_rate_admitted_total')]-before[key('q4t_client_rate_admitted_total')]
    tokens = after[key('q4t_client_rate_settled_tokens_total')]-before[key('q4t_client_rate_settled_tokens_total')]
    require(admitted == 3 and tokens > 0 and after[key('q4t_client_rate_reserved_tokens')] == 0,
            'one ASR and two frontend work items, actual settlement')
    require(all(after[k] == 0 for k in ('q4t_active_chats','q4t_realtime_sessions',
                'q4t_realtime_host_reserved_bytes')) and after['q4t_seq_slots_free'] == 1,
            'actual session/request/host resources released')
    normalize = lambda s: re.sub(r'[\s。，,.!！?？]', '', s).lower()
    quality = [dict(label='input_transcription', actual=''.join(e['delta'] for e in sources), expected=receipt['expected_input']),
               dict(label='project_version', actual=spoken['delta'], expected=record['version'])]
    for row in quality: row['status'] = 'PASS' if normalize(row['actual']) == normalize(row['expected']) else 'FAIL'
    return dict(protocol='PASS', quality=quality, admitted=admitted, settled_tokens=tokens,
        private_token_breakdown_not_exposed=True, activity_seconds=seconds[-1],
        speech_segments=segments, waveform_bytes=len(raw), waveform_sha256=hashlib.sha256(raw).hexdigest(),
        listener_heard_audio=False, session_id=started['session']['id'])
