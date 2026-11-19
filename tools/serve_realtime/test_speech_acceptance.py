"""Falsified audio, ownership, EOF and quality receipts must not certify a run."""
import base64
import copy
import io
import struct
import unittest
import wave

from tools.serve_realtime import speech_acceptance as stage
from tools.serve_realtime import speech_checks as c


def fixture(mode='main'):
    record = dict(path=str(stage.ROOT / 'docs/STATUS.md'), version='rev366', sha256='a' * 64)
    rows, events, retrievals, actions, quality = [], [], [], [], []
    labels = (['tool', 'tool_audio', 'pcm', 'mulaw', 'alaw', 'cancel', 'after_cancel']
              if mode == 'main' else ['quota_audio', 'quota_refused'])
    for i, label in enumerate(labels):
        response_id, item_id = f'resp_{i}', f'item_{i}'
        row = dict(id=response_id, status='cancelled' if label == 'cancel' else
                   'failed' if label == 'quota_refused' else 'completed', output=[],
                   usage=dict(input_tokens=100, output_tokens=101, total_tokens=201,
                              output_token_details=dict(audio_tokens=97, text_tokens=4)))
        if label == 'quota_refused':
            row['status_details'] = dict(error=dict(code='client_rpm_exhausted'))
        elif label == 'tool':
            item = dict(id=item_id, type='function_call', name='lookup_project_record',
                        status='completed', arguments='{"record_key":"release"}')
            row['output'] = [item]
            events.append(dict(type='response.function_call_arguments.delta',
                response_id=response_id, item_id=item_id, delta=item['arguments']))
        elif label == 'after_cancel':
            row['output'] = [dict(type='message', content=[dict(type='output_text', text='42')])]
            quality.append(dict(label=label, actual='42', expected='42', status='PASS'))
        else:
            fmt = {'mulaw': 'audio/pcmu', 'alaw': 'audio/pcma'}.get(label, 'audio/pcm')
            encoded = dict(type=fmt, **({'rate': 24000} if fmt == 'audio/pcm' else {}))
            raw = struct.pack('<h', 1024) * 24000 if fmt == 'audio/pcm' else b'\x91' * 8000
            transcript = record['version'] if label == 'tool_audio' else c.PHRASE
            part = dict(type='output_audio', transcript='' if label == 'cancel' else transcript)
            item = dict(id=item_id, type='message', role='assistant',
                        status='incomplete' if label == 'cancel' else 'completed', content=[part])
            row.update(output=[item], output_modalities=['audio'], audio=dict(output=dict(format=encoded)))
            position = dict(response_id=response_id, item_id=item_id, output_index=0, content_index=0)
            events.extend([dict(type='response.output_audio_transcript.delta',
                                delta=transcript, **position),
                           dict(type='response.output_audio.delta',
                                delta=base64.b64encode(raw).decode(), **position)])
            if label == 'cancel':
                events.append(dict(type='conversation.item.truncated', item_id=item_id,
                                   audio_end_ms=20, content_index=0))
                actions.append(dict(type='active_truncate', response_id=response_id,
                                    item_id=item_id, event_id='pending', milliseconds=20))
            events.extend([dict(type='response.output_audio.done', **position),
                           dict(type='response.output_audio_transcript.done',
                                transcript=part['transcript'], **position)])
            retained = copy.deepcopy(item)
            retained['content'][0]['audio'] = base64.b64encode(
                raw[:960] if label == 'cancel' else raw).decode()
            retrievals.append(dict(label=label, item=retained))
            if label != 'cancel':
                quality.append(dict(label=label, actual=transcript,
                                    expected=transcript, status='PASS'))
            if label == 'pcm':
                truncated = copy.deepcopy(retained)
                truncated['content'][0].update(audio=base64.b64encode(raw[:4800]).decode(), transcript='')
                retrievals.append(dict(label='pcm_truncated', item=truncated))
                actions.append(dict(type='deleted', item_id=item_id,
                                    error=dict(code='realtime_item_not_found')))
        rows.append(dict(label=label, response=row))
        events.append(dict(type='response.done', response=copy.deepcopy(row)))
    if mode == 'main':
        actions.append(dict(type='locked_voice', error=dict(code='realtime_voice_locked')))
        actions += [dict(type='speed', label=n, speed=s) for n, s in
                    [('pcm', 1), ('mulaw', 1), ('alaw', .75)]]
    for i, event in enumerate(events):
        event['event_id'] = f'event_{i}'
        if event['type'] == 'conversation.item.truncated':
            next(v for v in actions if v['type'] == 'active_truncate')['event_id'] = event['event_id']
    value = dict(status='PASS', mode=mode, transport='official-openai-wss',
        connections=1, close_code=1000, automatic_replay=False, response_creates=len(rows),
        responses=rows, events=events, retrievals=retrievals, checks=actions, quality=quality,
        tool_record=dict(record_key='release', version=record['version'],
                         source='docs/STATUS.md', source_sha256=record['sha256']))
    return value, record


class SpeechEvidence(unittest.TestCase):
    def reject(self, mutate, mode='main'):
        value, record = fixture(mode)
        mutate(value)
        with self.assertRaises(Exception): stage.check_receipt(value, mode, record)

    def test_actual_format_clocks_and_cumulative_single_owner_usage(self):
        for mode, total in [('main', 1407), ('quota', 201)]:
            value, record = fixture(mode)
            self.assertEqual(stage.check_receipt(value, mode, record)[0], total)

    def test_empty_changed_or_unaligned_waveform_is_not_accepted(self):
        for new in ['', 'AA==', '!!!!', base64.b64encode(b'changed').decode()]:
            self.reject(lambda v: v['retrievals'][0]['item']['content'][0].update(audio=new))

    def test_lost_replayed_and_premature_eof_are_not_accepted(self):
        self.reject(lambda v: v['events'].append(copy.deepcopy(v['events'][-1])))
        self.reject(lambda v: v['events'].__setitem__(slice(None),
            [e for e in v['events'] if e['type'] != 'response.output_audio.done']))
        def early(v):
            at = next(i for i, e in enumerate(v['events']) if e['type'] == 'response.output_audio.done')
            v['events'].insert(0, v['events'].pop(at))
        self.reject(early)

    def test_foreign_item_and_missing_transcript_delta_are_not_accepted(self):
        self.reject(lambda v: next(e for e in v['events'] if e['type'] ==
            'response.output_audio.delta').update(item_id='foreign'))
        self.reject(lambda v: next(e for e in v['events'] if e['type'] ==
            'response.output_audio_transcript.delta').update(delta='invented'))

    def test_unsupported_clock_and_missing_audio_usage_are_not_accepted(self):
        for field, new in [('audio_tokens', 0), ('audio_tokens', True), ('text_tokens', 999)]:
            self.reject(lambda v: v['responses'][1]['response']['usage']['output_token_details'].update({field: new}))
        self.reject(lambda v: v['responses'][1]['response']['audio']['output']['format'].update(rate=16000))

    def test_unplayed_text_and_late_audio_cannot_revive(self):
        self.reject(lambda v: next(r for r in v['retrievals'] if r['label'] ==
            'pcm_truncated')['item']['content'][0].update(transcript='unplayed'))
        def late(v):
            event = copy.deepcopy(next(e for e in v['events'] if
                e['type'] == 'response.output_audio.delta' and e['response_id'] == 'resp_5'))
            event['event_id'] = 'event_late'
            v['events'].append(event)
        self.reject(late)

    def test_wrong_owner_voice_speed_and_quota_are_not_accepted(self):
        self.reject(lambda v: next(c for c in v['checks'] if c['type'] ==
            'active_truncate').update(item_id='foreign'))
        self.reject(lambda v: next(c for c in v['checks'] if c['type'] ==
            'locked_voice')['error'].update(code='unrelated_failure'))
        self.reject(lambda v: next(c for c in v['checks'] if c.get('label') ==
            'alaw').update(speed=1))
        self.reject(lambda v: v['responses'][1]['response']['status_details']['error'].update(
            code='tts_unavailable'), mode='quota')

    def test_quality_failure_cannot_be_relabelled_or_omitted(self):
        self.reject(lambda v: v.update(quality=[]))
        self.reject(lambda v: v['quality'][0].update(actual='wrong', status='PASS'))
        self.reject(lambda v: v['tool_record'].update(version='rev0'))
        self.reject(lambda v: next(e for e in v['events'] if e['type'] ==
            'response.function_call_arguments.delta').update(delta='{}'))

    def test_strict_lease_scope_and_no_replay_limits(self):
        _, record = fixture()
        plan = dict(schema='q4t-realtime-speech-business-v1', limits=dict(stage.LIMITS),
                    contract=dict(stage.CONTRACT), record=record)
        stage.check_plan(plan)
        for key in ['starts', 'response_creates', 'seconds']:
            changed = copy.deepcopy(plan); changed['limits'][key] += 1
            with self.assertRaises(Exception): stage.check_plan(changed)
        self.reject(lambda v: v.update(automatic_replay=True))
        self.reject(lambda v: v.update(close_code=1006))

    def test_g711_oracle_wav_preserves_samples_and_wire_rate(self):
        for fmt, codes, expected in [('audio/pcmu', b'\x00\x80', [-32124, 32124]),
                                     ('audio/pcma', b'\x00\x80', [-5504, 5504])]:
            data, physical = c.waveform(codes * 800, fmt)
            with wave.open(io.BytesIO(data)) as wav:
                self.assertEqual((wav.getframerate(), wav.getnframes()), (8000, 1600))
                self.assertEqual(list(struct.unpack('<hh', wav.readframes(2))), expected)
            self.assertGreater(physical['peak'], .01)

    def test_silent_or_noncanonical_content_cannot_pass(self):
        for fmt, raw in [('audio/pcm', b'\0\0' * 24000),
                         ('audio/pcmu', b'\xff' * 8000), ('audio/pcma', b'\xd5' * 8000)]:
            with self.assertRaises(Exception): c.waveform(raw, fmt)
        for encoded in ['Zg===', 'Zh==', 'Zg==\n']:
            with self.assertRaises(Exception): c.decode64(encoded)

    def test_auxiliary_release_is_healthy_but_failures_are_not_hidden(self):
        for resident in [True, False]:
            health = dict(state='ready' if resident else 'metadata_ready',
                compute_failures=0, reason='none', core_readiness_dependency=False,
                device_bytes=1024 if resident else 0)
            stage.tts_health(dict(native_tts_health=health), resident)
            for key, value in [('state', 'cooldown'), ('compute_failures', 1),
                               ('reason', 'device'), ('core_readiness_dependency', True),
                               ('device_bytes', 0 if resident else 1024)]:
                with self.assertRaises(Exception):
                    stage.tts_health(dict(native_tts_health=dict(health, **{key: value})), resident)


if __name__ == '__main__': unittest.main()
