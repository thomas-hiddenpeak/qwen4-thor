"""Independent checks of actual Realtime audio events and retrieved bytes."""
import base64
import hashlib
import io
import struct
import wave

from tools.serve_service.package import require

PHRASE = 'Hello, this is a native speech test.'
FORMATS = {'audio/pcm': (24000, 2), 'audio/pcmu': (8000, 1),
           'audio/pcma': (8000, 1)}


def decode64(value):
    require(type(value) is str and len(value) <= 4 << 20, 'base64 byte bound')
    raw = base64.b64decode(value, validate=True)
    require(base64.b64encode(raw).decode() == value, 'noncanonical base64')
    return raw


def usage(row):
    value = row['usage']
    require(all(type(value[k]) is int and value[k] > 0 for k in
                ('input_tokens', 'output_tokens', 'total_tokens')) and
            value['total_tokens'] == value['input_tokens'] +
            value['output_tokens'], 'actual positive usage arithmetic')
    return value['total_tokens']


def audio(events, response, retrieved):
    require(response['status'] == 'completed' and
            response['output_modalities'] == ['audio'], 'complete audio required')
    usage(response)
    category = response['usage']['output_token_details']
    require(type(category['audio_tokens']) is int and category['audio_tokens'] > 0
            and type(category['text_tokens']) is int and category['text_tokens'] > 0
            and sum(category[k] for k in ('audio_tokens', 'text_tokens')) ==
            response['usage']['output_tokens'], 'actual audio/text accounting')
    messages = [v for v in response['output'] if v['type'] == 'message']
    require(len(messages) == 1, 'one actual speech item')
    item = messages[0]
    require(item['role'] == 'assistant' and item['status'] == 'completed' and
            len(item['content']) == 1, 'speech item state')
    part = item['content'][0]
    require(part['type'] == 'output_audio' and part['transcript'] and
            'audio' not in part, 'terminal must not repeat audio bytes')
    selected = [v for v in events if v.get('response_id') == response['id']]
    chunks = [v for v in selected if v['type'] == 'response.output_audio.delta']
    require(chunks, 'no actual audio delta')
    for event in chunks:
        require(event['item_id'] == item['id'] and event['output_index'] == 0
                and event['content_index'] == 0, 'foreign audio chunk')
    raw = b''.join(decode64(v['delta']) for v in chunks)
    done = [v for v in selected if v['type'] == 'response.output_audio.done']
    transcript = [v for v in selected if
                  v['type'] == 'response.output_audio_transcript.done']
    require(len(done) == len(transcript) == 1 and
            done[0]['item_id'] == transcript[0]['item_id'] == item['id'] and
            transcript[0]['transcript'] == part['transcript'] and
            all(k not in done[0] for k in ('audio', 'delta')), 'audio EOF identity')
    deltas = [v for v in selected if
              v['type'] == 'response.output_audio_transcript.delta']
    require(all(v['item_id'] == item['id'] for v in deltas) and
            ''.join(v['delta'] for v in deltas) == part['transcript'],
            'actual transcript stream differs')
    end = next(i for i, v in enumerate(events) if v['type'] == 'response.done'
               and v.get('response', {}).get('id') == response['id'])
    require(events.index(chunks[-1]) < events.index(done[0]) < end and
            events.index(transcript[0]) < end, 'premature audio completion')
    require(retrieved['id'] == item['id'] and
            retrieved['content'][0]['transcript'] == part['transcript'] and
            decode64(retrieved['content'][0]['audio']) == raw,
            'retrieved waveform differs from sent audio')
    fmt = response['audio']['output']['format']
    rate, width = FORMATS[fmt['type']]
    require(fmt == ({'type': 'audio/pcm', 'rate': 24000} if width == 2 else
                    {'type': fmt['type']}) and
            rate // 10 <= len(raw) // width <= rate * 30 and len(raw) % width == 0,
            'audio clock/extent')
    return raw, dict(format=fmt['type'], rate=rate, bytes=len(raw),
                    samples=len(raw) // width,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    transcript=part['transcript'])


def retained_prefix(item, raw, fmt, milliseconds):
    rate, width = FORMATS[fmt]
    require(type(milliseconds) is int and milliseconds >= 0,
            'integer playback clock')
    require(item['content'][0]['transcript'] == '' and
            decode64(item['content'][0]['audio']) ==
            raw[:milliseconds * rate // 1000 * width],
            'unplayed waveform/transcript revived')


def waveform(raw, fmt):
    """Development-only G.711 oracle; production conversion is native C++."""
    rate, width = FORMATS[fmt]
    require(raw and len(raw) % width == 0, 'complete samples required')
    if width == 1:
        import audioop
        raw = (audioop.ulaw2lin if fmt == 'audio/pcmu' else
               audioop.alaw2lin)(raw, 2)
    samples = struct.unpack('<' + 'h' * (len(raw) // 2), raw)
    peak = max(abs(v) for v in samples) / 32768
    rms = (sum(float(v) * v for v in samples) / len(samples)) ** .5 / 32768
    require(peak > .01 and rms > .0001, 'silent waveform cannot pass')
    out = io.BytesIO()
    with wave.open(out, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(raw)
    return out.getvalue(), dict(peak=peak, rms=rms, rate=rate,
                                samples=len(samples))
