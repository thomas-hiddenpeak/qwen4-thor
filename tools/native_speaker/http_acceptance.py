"""One start/10 creates: real speaker turns, references, SDKs and isolation."""
import argparse
import base64
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import wave

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_asr import alignment_http_acceptance as aligned
from tools.native_asr import transcription_acceptance as asr
from tools.serve_service.package import read, require, save, sha

WORK = ROOT/'.q4t-work/serve-diarization-20261118'
LIMITS = dict(starts=1, chats=10, controls=2000, seconds=600)
SPEAKER_SHA = '901bb7278ba2f4f43076854e1e2757d7ae8c15f429b16b42e35cd729c75754af'
VAD_SHA = 'c59271c284ae9c8335d795d60e0bfdb71aaaceec578d9bd9ffc1b8153c319ea1'


def requests():
    base = dict(model=asr.MODEL, language='en', response_format='diarized_json',
                chunking_strategy=dict(type='server_vad', threshold=.5,
                    prefix_padding_ms=300, silence_duration_ms=500))
    refs = [WORK/'voice-fixtures-01'/name for name in ('spk1_snt1.wav', 'spk2_snt1.wav')]
    known = dict(base, known_speaker_names=['first', 'second'],
        known_speaker_references=['data:audio/wav;base64,' +
            base64.b64encode(path.read_bytes()).decode() for path in refs])
    return dict(chat=asr.requests()['chat'], plain=base, known=known,
                stream=dict(base, stream=True))


def prepare(args):
    aligned.prepare(args)
    out = args.output.resolve()
    plan = read(out/'plan.json')
    fixed = read(WORK/'voice-fixtures-01/receipt.json')['files']
    samples = {}
    for row in fixed:
        path = WORK/'voice-fixtures-01'/row['name']
        require(sha(path) == row['sha256'], 'speaker fixture identity')
        with wave.open(str(path), 'rb') as audio:
            require(audio.getnchannels() == 1 and audio.getsampwidth() == 2 and
                    audio.getframerate() == 16000, 'fixed voice PCM shape')
            samples[row['name']] = audio.readframes(audio.getnframes())
        plan['clips'][row['name']] = dict(path=str(path), sha256=sha(path))
    sequences = dict(same=['spk1_snt1.wav', 'spk1_snt2.wav'],
                     returns=['spk1_snt1.wav', 'spk2_snt1.wav', 'spk1_snt2.wav'],
                     capacity=['spk1_snt1.wav'] * 10, silence=[])
    for name, sequence in sequences.items():
        padding, gap = (0, 0) if name == 'capacity' else (4800, 16000)
        raw = bytes(padding * 2)
        utterances = []
        for index, clip in enumerate(sequence):
            if index:
                raw += bytes(gap * 2)
            begin = len(raw) // 2
            raw += samples[clip]
            utterances.append(dict(begin_sample=begin, end_sample=len(raw)//2,
                                   voice='A' if clip.startswith('spk1_') else 'B'))
        raw += bytes(padding * 2)
        if name == 'silence':
            raw = bytes(32000 * 2)
        path = out/(name+'.wav')
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000)
            audio.writeframes(raw)
        plan['clips'][name] = dict(path=str(path), sha256=sha(path),
            samples=len(raw)//2, utterances=utterances)
    speaker = WORK/'native-assets-01'
    plan['speaker_directory'] = str(speaker)
    plan['speaker_assets'] = {n:dict(bytes=(speaker/n).stat().st_size,
        sha256=sha(speaker/n)) for n in ('config.json','model.safetensors')}
    require(plan['speaker_assets']['model.safetensors']['sha256'] == SPEAKER_SHA,
            'speaker weights identity')
    vad = aligned.WORK/'vad-01/silero_vad_16k.safetensors'
    require(sha(vad) == VAD_SHA, 'VAD weights identity')
    plan['vad_asset'] = dict(path=str(vad), sha256=VAD_SHA, bytes=vad.stat().st_size)
    for folder in ('tools/native_speaker','tools/native_asr'):
        for path in (ROOT/folder).glob('*'):
            if path.is_file():plan['sources'][str(path.relative_to(ROOT))] = sha(path)
    plan.update(limits=LIMITS, requests=requests(), scope=__doc__,
        planned=dict(starts=1, creates=10, successful_diarized=5,
                     rejected_diarized=2, cancelled_diarized=1, core_chats=2),
        command=['/usr/bin/python3','-B',str(Path(__file__).resolve()),'run',
                 '--plan',str(out/'plan.json')],
        quality=dict(labels='same A/A; different and returning A/B/A; references first/second/first',
            coverage='each actual source utterance has nonempty transcript and matching voice',
            timestamps='finite real global times within source plus 300ms VAD padding and 80ms quantization',
            limits='No full language quality, overlap separation or performance acceptance'),
        mutations=['private readonly asset hardlinks; reveal withheld speaker config only',
            'concatenate fixed labelled speech and explicit silence in owned WAV files',
            'close owned streaming socket; no restart/default/external service action'])
    save(out/'plan.json', plan)


def check_result(value, clip, names=None):
    require(value.get('task') == 'transcribe' and
            value.get('duration') == clip['samples']/16000, 'actual duration/task missing')
    rows = value['segments']
    require(isinstance(rows, list), 'missing diarized segment list')
    if not clip['utterances']:
        require(not rows and value['text'] == '', 'silence fabricated transcript')
        require(all(value['usage'][k] == 0 for k in
                    ('input_tokens','output_tokens','total_tokens')), 'silence fabricated work')
        return dict(silence=True)
    require(rows and value['text'].strip(), 'empty real transcript')
    usage = value['usage']
    require(usage['type'] == 'tokens' and all(type(usage[k]) is int and
        0 < usage[k] < 2**53 for k in ('input_tokens','output_tokens','total_tokens')),
        'missing actual diarized usage')
    require(usage['input_tokens']+usage['output_tokens'] == usage['total_tokens'],
            'actual diarized token sum')
    detail = usage['input_token_details']
    require(type(detail['audio_tokens']) is int and detail['audio_tokens'] > 0 and
            type(detail['text_tokens']) is int and detail['text_tokens'] >= 0 and
            detail['audio_tokens']+detail['text_tokens'] == usage['input_tokens'],
            'actual diarized audio/text usage')
    previous, seen, labels = 0, set(), []
    for index, row in enumerate(rows):
        require(row.get('id') == str(index) and row.get('type') == 'transcript.text.segment',
                'diarized ID/type contract')
        require(isinstance(row['text'], str) and row['text'].strip(), 'empty segment text')
        begin, end = row['start'], row['end']
        require(all(type(v) in (int,float) and math.isfinite(v) for v in (begin,end))
                and previous <= begin <= end <= clip['samples']/16000,
                'nonfinite or nonmonotonic actual segment time')
        previous = end
        overlaps = [max(0, min(end*16000,u['end_sample']) -
                              max(begin*16000,u['begin_sample'])) for u in clip['utterances']]
        require(max(overlaps) > 0, 'transcript does not cover a real utterance')
        owner = max(range(len(overlaps)), key=overlaps.__getitem__)
        utterance = clip['utterances'][owner]
        require(begin*16000 >= utterance['begin_sample']-6080 and
                end*16000 <= utterance['end_sample']+6080,
                'segment spans unrelated voice or invented source time')
        expected = (names or {}).get(utterance['voice'], utterance['voice'])
        require(row['speaker'] == expected, 'actual voice assignment differs')
        seen.add(owner);labels.append(row['speaker'])
    require(seen == set(range(len(clip['utterances']))), 'actual utterance dropped')
    compact = lambda s:re.sub(r'\s+', '', s)
    require(compact(''.join(r['text'] for r in rows)) == compact(value['text']),
            'segment text differs from complete transcript')
    return dict(labels=labels, covered_utterances=len(seen), text=value['text'])


def check_stream(value, clip):
    events = value['events']
    require(events and events[-1]['type'] == 'transcript.text.done' and
            sum(e['type'] == 'transcript.text.done' for e in events) == 1,
            'missing or duplicated stream terminal')
    final = events[-1]
    checked = check_result(final, clip)
    segments, delta, segment_ids = [], [], []
    for event in events[:-1]:
        if event['type'] == 'transcript.text.delta':
            delta.append(event['delta']);segment_ids.append(event['segment_id'])
        elif event['type'] == 'transcript.text.segment':
            segments.append(event)
        else:raise RuntimeError('unexpected diarized event')
    require(''.join(delta) == final['text'] and
            segment_ids == [s['id'] for s in segments], 'stream delta/segment linkage')
    for observed, expected in zip(segments, final['segments']):
        require(all(observed[k] == expected[k] for k in
                    ('id','type','start','end','speaker','text')), 'stream final segment differs')
    require(len(segments) == len(final['segments']), 'stream segment dropped')
    return checked


class Stage(aligned.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_NATIVE_DIARIZED_HTTP_SDK_REFERENCES_SCOPED_RECOVERY'

    def additional_options(self):
        return ['--speaker-model-dir',str(self.out/'speaker-assets'),
                '--speaker-max-windows','8','--vad-model-dir',str(self.out/'vad-assets')]

    def start(self):
        speaker = self.out/'speaker-assets';speaker.mkdir(mode=0o700)
        for name, record in self.plan['speaker_assets'].items():
            source = Path(self.plan['speaker_directory'])/name
            require(sha(source) == record['sha256'] and source.stat().st_size == record['bytes'],
                    'speaker asset drift')
            os.link(source,speaker/(name+'.withheld' if name == 'config.json' else name))
        vad = self.out/'vad-assets';vad.mkdir(mode=0o700)
        source = Path(self.plan['vad_asset']['path'])
        require(sha(source) == self.plan['vad_asset']['sha256'], 'VAD asset drift')
        os.link(source,vad/source.name)
        super().start()
        aligner = self.out/'aligner-assets'
        os.rename(aligner/'model.safetensors.withheld',aligner/'model.safetensors')
        require(read(self.out/'capabilities.json')['transcriptions']['speaker']['configured'],
                'speaker configuration missing')

    def state(self, label):
        require(self.http('/readyz',label=label+'-ready')[0] == 200,
                'speaker failure poisoned core readiness')
        status, raw = self.http('/v1/config',label=label+'-config')
        require(status == 200, 'speaker health absent')
        self.same_model()
        return json.loads(raw)['native_speaker_health']

    def business(self):
        clips, rows = self.plan['clips'], self.plan['requests']
        fields = [('model',asr.MODEL),('response_format','diarized_json'),('language','en')]
        audio = Path(clips['returns']['path']).read_bytes()
        status, raw = self.multipart('missing-speaker',fields,audio)
        require(status == 503 and json.loads(raw)['error']['code'] == 'speaker_assets_invalid',
                'missing speaker assets misclassified')
        failed_at = time.monotonic()
        state = self.state('missing')
        require(state['state'] == 'degraded' and state['load_failures'] == 1,
                'speaker failure not isolated')
        self.core('core-during-speaker-failure')
        assets = self.out/'speaker-assets'
        os.rename(assets/'config.json.withheld',assets/'config.json')
        time.sleep(max(0,5.1-(time.monotonic()-failed_at)))
        quality = {}
        for label,clip,request,python,names in [
            ('python-same','same',rows['plain'],True,None),
            ('js-returning','returns',rows['plain'],False,None),
            ('python-known','returns',rows['known'],True,dict(A='first',B='second'))]:
            value = self.sdk(label,clip,request,python=python)
            quality[label] = check_result(value,clips[clip],names)
            check_result(read(self.out/(label+'.wire')),clips[clip],names)
        state = self.state('recovered')
        require(state['state'] == 'ready' and state['load_failures'] == 1 and
                state['compute_failures'] == 0, 'same-PID speaker recovery missing')
        streamed = self.sdk('js-stream','returns',rows['stream'])
        quality['stream'] = check_stream(streamed,clips['returns'])
        require(b'[DONE]' not in (self.out/'js-stream.wire').read_bytes(),'Chat sentinel in ASR')
        status, raw = self.multipart('window-capacity',fields,
                                     Path(clips['capacity']['path']).read_bytes())
        require(status == 400 and json.loads(raw)['error']['code'] == 'speaker_window_limit',
                'speaker window capacity dropped audio or silently merged voices')
        silent = self.sdk('python-silence','silence',rows['plain'],python=True)
        quality['silence'] = check_result(silent,clips['silence'])
        before = self.drained('cancel-before')
        self.multipart('cancel-diarized',fields+[('stream','true')],audio,cancel=True)
        after = self.drained('cancel-after')
        require(after['q4t_requests_aborted_total'] == before['q4t_requests_aborted_total']+1,
                'actual diarized cancellation not observed')
        self.core('core-after-speaker')
        state = self.state('final')
        require(state['state'] == 'ready' and state['compute_failures'] == 0,
                'final speaker health differs')
        require(self.counts['chats'] == 10,'attempt accounting differs')
        save(self.out/'quality.json',quality)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode',required=True)
    prepare_parser = sub.add_parser('prepare')
    prepare_parser.add_argument('--binary',type=Path,required=True)
    prepare_parser.add_argument('--output',type=Path,required=True)
    prepare_parser.add_argument('--port',type=int,default=18246)
    run = sub.add_parser('run');run.add_argument('--plan',type=Path,required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':prepare(args)
    else:
        with asr.base.lifecycle.child_subreaper():Stage(read(args.plan)).run()


if __name__ == '__main__':main()
