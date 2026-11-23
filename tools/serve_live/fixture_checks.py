"""Verify fixture bytes separately, then retain the original conversation checks."""
import base64
import hashlib
from tools.serve_service.package import require
from tools.serve_live.checks import observe as conversation, speech_health


def observe(receipt, journal, before, after, record, clip, *, store=False):
    rows = [r for r in journal if r['direction'] == 'server']
    events = [r['event'] for r in rows]
    require(events == receipt['events'], 'complete original fixture journal')
    playing = [i for i,e in enumerate(events) if e['type'] == 'session.output_audio.fixture.playing']
    played = [i for i,e in enumerate(events) if e['type'] == 'session.output_audio.fixture.played']
    require(len(playing) == len(played) == 1 and 0 < playing[0] < played[0] < len(events)-1,
            'one actual accepted and drained fixture')
    begin,end = playing[0],played[0]
    require(events[begin]['client_event_id'] == events[end]['client_event_id'] == 'fixture_audio',
            'fixture command correlation')
    ids = [e['event_id'] for e in events if e['type'] != 'session.output_audio.delta']
    require(all(ids) and len(set(ids)) == len(ids), 'unique actual fixture and conversation IDs')
    raw = bytearray(); removed = {begin,end}
    for i in range(begin+1,end):
        e = events[i]
        require(e['type'] in ('session.output_audio.delta','session.usage.updated',
                             'session.input_audio.muted'),
                'no inference during fixture playback')
        if e['type'] == 'session.output_audio.delta':
            require(set(e) == {'type','delta'}, 'actual fixture audio event')
            part = base64.b64decode(e['delta'], validate=True)
            require(part and len(part)%2 == 0, 'complete fixture PCM chunks')
            raw.extend(part);removed.add(i)
            require(len(raw) <= 30*48000, 'fixture output byte bound')
    require(len(raw) == clip['bytes'] and hashlib.sha256(raw).hexdigest() == clip['sha256'],
            'actual PCM24 output equals entire validated source')
    proof = receipt['fixture']
    require(proof == dict(sha256=clip['sha256'],bytes=clip['bytes'],played=True,listener_heard=False),
            'bounded fixture receipt cannot claim listener completion')
    sent = [r['event'] for r in journal if r['direction'] == 'client']
    command = [e for e in sent if e['type'] == 'session.output_audio.fixture.play']
    require(len(command) == 1 and command[0]['event_id'] == 'fixture_audio' and
            base64.b64decode(command[0]['audio'],validate=True) == raw,
            'actual source was supplied in the play command')
    for name in ('fixture_drop','fixture_muted'):
        probe = [e for e in sent if e.get('event_id') == name]
        require(len(probe) == 1 and probe[0]['type'] == 'session.input_audio.append' and
                base64.b64decode(probe[0]['audio'],validate=True) == raw + bytes(48000),
                'actual voiced input probe during playback and preserved mute')
    def position(direction, kind=None, event_id=None):
        found = [i for i,row in enumerate(journal)
                 if row['direction'] == direction and
                 (kind is None or row['event']['type'] == kind) and
                 (event_id is None or row['event'].get('event_id') == event_id)]
        require(len(found) == 1, 'unique fixture probe ordering witness')
        return found[0]
    first = position('server','session.output_audio.fixture.playing')
    last = position('server','session.output_audio.fixture.played')
    require(first < position('client',event_id='fixture_drop') < last and
            first < position('client','session.input_audio.mute') < last and
            last < position('client',event_id='fixture_muted') <
            position('client','session.input_audio.unmute'),
            'voice probes actually cover playback drop and persistent mute')
    # Do not alter originals. Only the separately verified fixture output is
    # excluded from the unchanged model-speech observer's transcript pairing.
    view = [];position = 0
    for row in journal:
        if row['direction'] == 'server':
            keep = position not in removed;position += 1
            if not keep:continue
        view.append(row)
    filtered = dict(receipt,events=[e for i,e in enumerate(events) if i not in removed])
    result = conversation(filtered,view,before,after,record,store=store)
    result['fixture'] = dict(bytes=len(raw),sha256=clip['sha256'],source_equal=True,
        new_model_work=0,listener_heard=False,
        receive_interval_ns=int(rows[end]['monotonic_ns'])-int(rows[begin]['monotonic_ns']))
    require(result['fixture']['receive_interval_ns'] >= 0, 'monotonic fixture journal')
    return result
