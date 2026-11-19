"""Bounded real registration -> persisted ICL -> official Speech/Chat SDK.

Reuses the already qualified native TLS and lifecycle driver. The admission
peer is a PID-bound private fixture; this does not qualify Guardian recovery.
No model/codec is supplied by Python, and transcription receives no text hint.
"""
import base64
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_tts import voice_sdk_acceptance as sdk_driver
from tools.native_tts import voice_service_acceptance as direct
from tools.serve_protocol.verify import metrics

read, save, sha, require = direct.read, direct.save, direct.sha, direct.require
LIMITS = dict(starts=1, ingress_starts=1, posts=9, controls=200, seconds=900,
              response_bytes=4 << 20, artifact_mib=64, disk_headroom_mib=256)
TEXT = dict(speech='The blue train arrives at noon.',
            chat='你好，会议下午三点开始。', reference=direct.SAMPLE)
CODEC = '836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258'
CONTRACT = dict(reference_samples=107520, reference_frames=56,
                no_asr_text_or_language_hint=True, one_core_pid=True,
                listening='PENDING_OWNER_DEFERRED', performance='NOT_CLAIMED',
                guardian_recovery_retested=False, exact_code_failure_preserved=True)


def validate_plan(phase):
    require(phase['format'] == 'q4t-icl-service-v1' and
            phase['bounds'] == LIMITS and phase['text'] == TEXT and
            phase['contract'] == CONTRACT, 'frozen ICL service plan differs')
    direct.validate_plan(phase['http'])
    require(phase['sdk']['version'] == sdk_driver.voice_sdk.VERSION,
            'SDK version differs')
    counts = phase['input_tokens']
    require(set(counts) == {'speech', 'chat'} and all(
        type(value) is int and 906 <= value <= 1408 for value in counts.values()),
        'independent frozen token count missing')


def check_reference(record, consent, voice, clip):
    require(record.get('kind') == 'voice' and record.get('id') == voice and
            record.get('owner') == consent['owner'], 'voice ownership differs')
    data = record['data']
    require(data['consent'] == consent['id'] and
            data['consent_source_sha256'] == consent['data']['source_sha256'] and
            data['source_sha256'] == clip['sha256'] and
            data['transcript'] == TEXT['reference'], 'reference provenance differs')
    reference = data.get('reference', {})
    require(set(reference) == {'format', 'codec_sha256', 'sample_rate', 'samples', 'codes'} and
            reference['format'] == 'q4t.tts-reference.v1' and
            reference['codec_sha256'] == CODEC and reference['sample_rate'] == 24000 and
            reference['samples'] == CONTRACT['reference_samples'] and
            data['duration_ns'] == CONTRACT['reference_samples'] * 1000000000 // 24000,
            'persisted reference format or length differs')
    codes = reference['codes']
    require(type(codes) is list and len(codes) == CONTRACT['reference_frames'] and
            all(type(row) is list and len(row) == 16 and all(
                type(code) is int and 0 <= code < 2048 for code in row) for row in codes),
            'reference must contain all actual code groups')
    condition = data['condition']
    require(type(condition) is list and len(condition) == 2048 and
            all(type(x) in (int, float) and math.isfinite(x) for x in condition) and
            sum(x*x for x in condition) > 0, 'speaker condition missing')
    return dict(samples=reference['samples'], frames=len(codes), codes=len(codes)*16,
                source_sha256=data['source_sha256'], codec_sha256=CODEC,
                transcript=data['transcript'])


def check_usage(usage, expected_input, samples):
    require(set(usage) == {'input_tokens', 'output_tokens', 'total_tokens'} and
            all(type(value) is int for value in usage.values()), 'actual usage fields differ')
    require(samples % 1920 == 0 and 2 <= samples // 1920 < 200 and
            usage['input_tokens'] == expected_input and
            usage['output_tokens'] == samples // 1920 * 16 + 1 and
            usage['total_tokens'] == expected_input + usage['output_tokens'],
            'reference input or natural output usage differs')


def check_settlement(before, after, tokens):
    key = lambda name: name + '{client="audio"}'
    require(type(tokens) is int and tokens > 0 and
            after[key('q4t_client_rate_settled_tokens_total')] -
            before[key('q4t_client_rate_settled_tokens_total')] == tokens and
            after[key('q4t_client_rate_admitted_total')] -
            before[key('q4t_client_rate_admitted_total')] == 1 and
            after[key('q4t_client_rate_reserved_tokens')] == 0,
            'real request settlement differs from actual public usage')


class Stage(sdk_driver.Stage):
    def __init__(self, phase):
        validate_plan(phase)
        self.phase, self.edge = phase, None
        # Keep the legacy base envelope intact, then impose the stronger limits
        # in budget/run below. All inherited network operations call budget.
        direct.Stage.__init__(self, phase['http'])
        self.counts['ingress_starts'] = 0
        for path, digest in phase['sources'].items():
            require(sha(ROOT/path) == digest, 'ICL driver source drift: '+path)
        for row in [phase['ingress'], *phase['tls_inputs'].values(),
                    *phase['clips'].values()]:
            require(sha(row['path']) == row['sha256'], 'frozen ICL fixture drift')
        for path, digest in phase['sdk']['files'].items():
            require(sha(path) == digest, 'SDK dependency drift: '+path)

    def budget(self, kind):
        require(time.monotonic()-self.started < LIMITS['seconds']-40,
                'ICL service deadline')
        require(self.counts[kind] < LIMITS[kind], 'ICL attempt bound: '+kind)
        self.artifacts()
        self.counts[kind] += 1
        save(self.out/'counts.json', self.counts)

    def rate(self, label):
        status, raw = self.http('/metrics', label=label)
        require(status == 200, 'rate observation unavailable')
        return metrics(raw.decode())

    def persisted(self, consent, voice):
        records = {}
        for path in sorted((self.out/'objects').rglob('*.json')):
            require(path.stat().st_size <= 4 << 20, 'object evidence bound')
            row = read(path).get('record', {})
            if row.get('id') in (consent, voice):
                require(row['id'] not in records, 'duplicate object evidence')
                records[row['id']] = row
        require(set(records) == {consent, voice}, 'live voice evidence missing')
        checked = check_reference(records[voice], records[consent], voice,
                                  self.phase['clips']['sample'])
        save(self.out/'reference.json', checked)
        return checked

    def transcribe_sdk(self, label, path, expected):
        body = self.plan['requests']['transcription']
        require(set(body) == {'model', 'response_format'}, 'ASR hint forbidden')
        raw, receipt = self.sdk(label, 'transcribe', body,
            clip=dict(path=str(path), sha256=sha(path)))
        value = json.loads(raw)
        require(receipt['result'] == value, 'SDK changed ASR result')
        save(self.out/(label+'.quality.json'), dict(expected=expected, response=value))
        direct.check_transcript(value, expected)

    def business(self):
        self.edge = sdk_driver.Edge(self)
        raw, _ = self.sdk('consent', 'consent_create', self.plan['requests']['consent'],
                          clip=self.phase['clips']['consent'])
        consent = direct.checked_metadata(200, raw, 'voice_consent')['id']
        raw, _ = self.sdk('voice', 'voice_create',
            dict(self.plan['requests']['voice'], consent=consent), clip=self.phase['clips']['sample'])
        voice = direct.checked_metadata(200, raw, 'voice')['id']
        self.persisted(consent, voice)
        speech = dict(model=direct.BASE, input=TEXT['speech'], voice=dict(id=voice),
                      response_format='wav', stream_format='sse')
        self.sdk('speech-other', 'speech', speech, client='other', status=404)
        before = self.rate('speech-rate-before')
        raw, _ = self.sdk('speech', 'speech', speech)
        audio, result = direct.speech.sse(raw)
        waveform = direct.check_wave(audio)
        check_usage(result['usage'], self.phase['input_tokens']['speech'], waveform['samples'])
        check_settlement(before, self.rate('speech-rate-after'), result['usage']['total_tokens'])
        save(self.out/'speech.audio.json', dict(**waveform, **result))
        path = self.out/'speech.wav'; path.write_bytes(audio)
        self.transcribe_sdk('speech-asr', path, TEXT['speech'])

        body = direct.chat.request('请严格原样复述，不要添加其他文字：'+TEXT['chat'],
                                   'wav', stream=True)
        body['audio']['voice'] = dict(id=voice); body['store'] = True
        before = self.rate('chat-rate-before')
        raw, receipt = self.sdk('chat', 'chat', body)
        states, usage, events = direct.chat.parse_stream(raw, 1)
        direct.chat.check_usage(usage, 1)
        require(states[0]['transcript'] == TEXT['chat'], 'Chat target differs')
        require(len(receipt['result']['events']) == len(events), 'SDK omitted SSE event')
        for a, b in zip(receipt['result']['events'], events, strict=True):
            require(a['choices'] == b['choices'], 'SDK changed SSE choices')
        audio = base64.b64decode(states[0]['data'], validate=True)
        waveform = direct.check_wave(audio)
        synthesis = usage['q4t_audio_synthesis']
        check_usage(dict(synthesis, total_tokens=sum(synthesis.values())),
                    self.phase['input_tokens']['chat'], waveform['samples'])
        check_settlement(before, self.rate('chat-rate-after'), usage['total_tokens'])
        save(self.out/'chat.audio.json', dict(**waveform, usage=usage))
        path = self.out/'chat.wav'; path.write_bytes(audio)
        self.transcribe_sdk('chat-asr', path, TEXT['chat'])
        completion = events[0]['id']
        raw, _ = self.sdk('chat-history', 'chat_retrieve', dict(completion_id=completion), control=True)
        require(json.loads(raw)['choices'][0]['message']['audio']['id'] == states[0]['id'],
                'persisted Chat audio differs')
        self.snapshot(consent, voice, completion)
        raw, _ = self.sdk('revoke', 'consent_delete', dict(consent_id=consent), control=True)
        require(json.loads(raw).get('deleted') is True, 'consent revoke failed')
        self.sdk('revoked-speech', 'speech', speech, status=404)
        raw, _ = self.sdk('delete-voice', 'voice_delete', dict(voice_id=voice), control=True)
        require(json.loads(raw).get('deleted') is True, 'voice delete failed')
        self.core('core-after')
        require(self.counts['posts'] == LIMITS['posts'], 'incomplete ICL business checklist')

    def run(self):
        result = dict(status='FAILED')
        old = signal.signal(signal.SIGALRM,
            lambda *_: (_ for _ in ()).throw(TimeoutError('ICL service deadline')))
        signal.alarm(LIMITS['seconds']-40)
        try:
            direct.chat.probe_ports((self.phase['edge_port'],))
            self.start(); self.business()
            result['status'] = 'PASS_REGISTERED_ICL_OFFICIAL_SDK_NATIVE_TLS'
        except BaseException:
            result['error'] = traceback.format_exc()
            raise
        finally:
            signal.alarm(0); signal.signal(signal.SIGALRM, old)
            try:
                if self.edge: self.edge.close()
            except BaseException:
                result['status'] = 'FAILED'
                result['entry_cleanup_error'] = traceback.format_exc()
                raise
            finally:
                try:
                    self.cleanup(); direct.chat.probe_ports((self.phase['edge_port'],))
                    require(direct.defaults() == self.plan['defaults'], 'default drift')
                except BaseException:
                    result['status'] = 'FAILED'; result['cleanup_error'] = traceback.format_exc()
                    raise
                finally:
                    result.update(counts=self.counts, seconds=time.monotonic()-self.started,
                        default_replaced=False, **CONTRACT)
                    save(self.out/'result.json', result)


def main():
    phase = read(Path(sys.argv[1])); validate_plan(phase)
    Path(phase['http']['output']).mkdir(mode=0o700, exist_ok=False)
    with direct.lifecycle.child_subreaper(): Stage(phase).run()


if __name__ == '__main__': main()
