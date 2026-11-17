"""One start/12 creates: real VAD chunks, SDK output, limits and isolation."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import wave
import zlib

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_asr import alignment_http_acceptance as aligned
from tools.native_asr import segment_http_acceptance as segment
from tools.native_asr import transcription_acceptance as asr
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, chats=12, controls=2000, seconds=600)
WEIGHT = 'silero_vad_16k.safetensors'
WEIGHT_SHA = 'c59271c284ae9c8335d795d60e0bfdb71aaaceec578d9bd9ffc1b8153c319ea1'
OUTPUT_BUDGET = 24


def requests():
    base = dict(model=asr.MODEL)
    vad = dict(type='server_vad', threshold=.5,
               prefix_padding_ms=300, silence_duration_ms=500)
    return dict(chat=asr.requests()['chat'], plain=asr.requests()['zh'],
        combined=dict(base, response_format='verbose_json',
                      timestamp_granularities=['word', 'segment'], chunking_strategy=vad),
        streamed=dict(base, stream=True, include=['logprobs'], chunking_strategy='auto'),
        subtitles=dict(base, response_format='vtt', chunking_strategy=vad),
        high_threshold=dict(base, chunking_strategy=dict(vad, threshold=1)),
        silence=dict(base, chunking_strategy='auto'))


def prepare(args):
    aligned.prepare(args)
    out = args.output.resolve()
    plan = read(out/'plan.json')
    asset = aligned.WORK/'vad-01'/WEIGHT
    require(asset.stat().st_size == 1239748 and sha(asset) == WEIGHT_SHA,
            'fixed CPU VAD asset drift')
    plan['vad_asset'] = dict(path=str(asset), bytes=1239748, sha256=WEIGHT_SHA)
    source = Path(plan['clips']['zh']['path'])
    with wave.open(str(source), 'rb') as audio:
        require(audio.getnchannels() == 1 and audio.getsampwidth() == 2 and
                audio.getframerate() == 16000, 'source PCM fixture shape')
        samples = audio.getnframes()
        pcm = audio.readframes(samples)
    for count in (2, 3, 4):
        path = out/f'repeated-{count}.wav'
        padding, gap = 8000, 24000
        wire = bytes(padding * 2) + bytes(gap * 2).join([pcm] * count) + bytes(padding * 2)
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
            audio.writeframes(wire)
        plan['clips'][f'repeated{count}'] = dict(path=str(path), sha256=sha(path),
            duration_ms=len(wire)//32, samples=len(wire)//2,
            utterances=[dict(begin_sample=padding + i*(samples+gap),
                              end_sample=padding + i*(samples+gap)+samples)
                        for i in range(count)], source_sha256=sha(source))
    silence = out/'silence.wav'
    with wave.open(str(silence), 'wb') as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
        audio.writeframes(bytes(16384*2))
    plan['clips']['silence'] = dict(path=str(silence), sha256=sha(silence), duration_ms=1024)
    plan.update(limits=LIMITS, requests=requests(), scope=__doc__,
        planned=dict(starts=1, creates=12, successful_asr=6, cancelled_asr=1,
                     rejected_asr=3, core_chats=2),
        runtime=dict(output_budget=OUTPUT_BUDGET, max_chunks=3),
        quality=dict(repeated_zh='each actual utterance exact, display spaces only',
            timestamps='monotonic real source offsets within original audio',
            chunks='2 speech clips; 4 speech clips exceed explicit max3',
            budgets='three clips cannot exceed cumulative24 output tokens',
            silence='explicit threshold1 and actual zero PCM produce no text/tokens',
            limits='No human timing accuracy, full quality or performance claim'),
        mutations=['private readonly asset links; reveal withheld VAD link only',
                   'create WAV fixtures from retained real PCM and explicit silence',
                   'close own streaming socket; no restart or default changes'])
    save(out/'plan.json', plan)


def check_text(text, count=2):
    expected = ' '.join([asr.EXPECTED['zh']]*count)
    require(text == expected, 'actual multi-utterance Chinese differs')
    return dict(text=text, utterances=count, exact=True)


def check_subtitle_text(text, _language):
    require(text == asr.EXPECTED['zh'] * 2, 'subtitle content lost real utterance')
    return dict(text=text, utterances=2, exact=True)


def check_empty(value):
    require(value['text'] == '' and value['usage']['type'] == 'tokens',
            'VAD silence fabricated text')
    usage = value['usage']
    require(all(usage[name] == 0 for name in ('input_tokens', 'output_tokens', 'total_tokens'))
            and usage['input_token_details'] == dict(audio_tokens=0, text_tokens=0),
            'VAD silence fabricated model work')


def check_combined(value, clip, vocab):
    check_text(value['text'])
    require(value['language'] == 'Chinese' and value['duration'] == clip['duration_ms']/1000
            and value['usage'] == dict(type='duration', seconds=clip['duration_ms']/1000),
            'full source duration/language missing')
    rows = value['segments']
    require(len(rows) == 2, 'VAD did not produce two independent speech clips')
    previous = 0
    for i, (row, source) in enumerate(zip(rows, clip['utterances'])):
        require(row['id'] == i and row['text'] == asr.EXPECTED['zh'],
                'global ID or original segment text differs')
        for name in ('start', 'end', 'avg_logprob', 'temperature',
                     'compression_ratio', 'no_speech_prob'):
            require(type(row[name]) in (int, float) and math.isfinite(row[name]),
                    'nonfinite actual segment field')
        require(type(row['seek']) is int and
                max(0, source['begin_sample']-4800)//160 <= row['seek'] < source['end_sample']//160,
                'seek not a real source frame offset')
        require(previous <= row['start'] <= row['end'] <= clip['duration_ms']/1000 and
                row['start'] >= row['seek'] / 100, 'segment time offset invalid')
        previous = row['end']
        require(row['tokens'] and all(type(v) is int and 0 <= v < 151936 for v in row['tokens']),
                'actual selected token IDs missing')
        require(b''.join(segment.token_bytes(row['tokens'], vocab)).decode() == row['text'],
                'selected IDs differ from real segment text')
        require(row['avg_logprob'] <= 0 and 0 <= row['temperature'] <= 1 and
                0 <= row['no_speech_prob'] < .5, 'segment statistic range')
        text = row['text'].encode()
        require(abs(row['compression_ratio'] - len(text)/len(zlib.compress(text, 6))) < 1e-12,
                'actual segment compression differs')
    require(len(value['words']) == 26, 'missing duplicated real word boundaries')
    previous = 0
    for word in value['words']:
        require(all(type(word[k]) in (int, float) and math.isfinite(word[k]) for k in ('start', 'end'))
                and previous <= word['start'] <= word['end'] <= clip['duration_ms']/1000,
                'nonmonotonic or invalid word time')
        previous = word['end']
    for i, row in enumerate(rows):
        words = value['words'][i*13:(i+1)*13]
        require(row['start'] == words[0]['start'] and row['end'] == words[-1]['end'],
                'segment extent differs from actual words')
    return dict(segments=rows, words=len(value['words']), human_timing_accuracy_assessed=False)


def check_stream(value, wire):
    events = value['events']
    require(events and events[-1]['type'] == 'transcript.text.done' and
            all(v['type'] == 'transcript.text.delta' for v in events[:-1]) and
            b'[DONE]' not in wire, 'incorrect transcription terminal')
    final = events[-1]
    check_text(final['text'])
    require(''.join(v['delta'] for v in events[:-1]) == final['text'],
            'cross-clip streamed text differs from final')
    require([p for v in events[:-1] for p in v.get('logprobs', [])] == final['logprobs'],
            'streamed actual probabilities differ')
    asr.usage(final, sdk=True)
    require(final['usage']['output_tokens'] <= OUTPUT_BUDGET, 'shared output ceiling bypassed')
    return dict(events=len(events), final=final)


class Stage(aligned.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_NATIVE_VAD_MULTICLIP_SDK_BUDGETS_SCOPED_RECOVERY'

    def additional_options(self):
        return ['--vad-model-dir', str(self.out/'vad-assets'), '--vad-max-chunks', '3',
                '--asr-max-output-tokens', str(OUTPUT_BUDGET)]

    def start(self):
        assets = self.out/'vad-assets'
        assets.mkdir(mode=0o700)
        record = self.plan['vad_asset']
        require(sha(record['path']) == record['sha256'] == WEIGHT_SHA and
                Path(record['path']).stat().st_size == record['bytes'], 'VAD asset drift')
        os.link(record['path'], assets/(WEIGHT+'.withheld'))
        super().start()
        assets = self.out/'aligner-assets'
        os.rename(assets/'model.safetensors.withheld', assets/'model.safetensors')
        caps = read(self.out/'capabilities.json')['transcriptions']
        require(caps['vad']['configured'] and caps['vad']['max_chunks'] == 3 and
                caps['vad']['backend'] == 'native_silero_cpu', 'native VAD capability missing')

    def states(self, label):
        status, _ = self.http('/readyz', label=label+'-ready')
        require(status == 200, 'VAD failure poisoned core readiness')
        status, raw = self.http('/v1/config', label=label+'-config')
        require(status == 200, 'private backend state unavailable')
        self.same_model()
        return {k: json.loads(raw)['native_'+k+'_health'] for k in ('vad', 'asr', 'alignment')}

    def token_counters(self, label):
        self.drained(label+'-drain')
        status, raw = self.http('/metrics', label=label)
        require(status == 200, 'actual counters unavailable')
        values = {}
        for name in ('prompt_tokens', 'generation_tokens'):
            matches = re.findall(r'^q4t_'+name+r'_total (\d+)\s*$', raw.decode(), re.M)
            require(len(matches) == 1, 'actual counter missing: '+name)
            values[name] = int(matches[0])
        return values

    def business(self):
        rows, clips = self.plan['requests'], self.plan['clips']
        zh = Path(clips['zh']['path']).read_bytes()
        fields = [('model', asr.MODEL), ('chunking_strategy', 'auto')]
        status, raw = self.multipart('missing-vad', fields, zh)
        require(status == 503 and json.loads(raw)['error']['code'] == 'vad_assets_invalid',
                'VAD missing asset not classified')
        failed_at = time.monotonic()
        state = self.states('missing-vad')
        require(state['vad']['state'] == 'degraded' and state['vad']['load_failures'] == 1
                and state['asr']['state'] == 'metadata_ready', 'VAD fault not isolated')
        value = self.sdk('python-plain-during-vad-failure', 'zh', rows['plain'], python=True)
        asr.quality(value['text'], 'zh'); asr.usage(value, sdk=True)
        self.core('core-during-vad-failure')
        assets = self.out/'vad-assets'
        os.rename(assets/(WEIGHT+'.withheld'), assets/WEIGHT)
        time.sleep(max(0, 5.1-(time.monotonic()-failed_at)))
        quality = {}
        value = self.sdk('python-repeated-segments', 'repeated2', rows['combined'], python=True)
        vocab = segment.read_vocab(self.plan)
        quality['segments'] = check_combined(value, clips['repeated2'], vocab)
        check_combined(read(self.out/'python-repeated-segments.wire'), clips['repeated2'], vocab)
        value = self.sdk('js-repeated-stream', 'repeated2', rows['streamed'])
        quality['stream'] = check_stream(value, (self.out/'js-repeated-stream.wire').read_bytes())
        value = self.sdk('js-repeated-vtt', 'repeated2', rows['subtitles'])
        require(value == (self.out/'js-repeated-vtt.wire').read_text() and
                value.count('WEBVTT') == 1, 'subtitle wire/header differs')
        # Reuse the cue parser, with the same real utterance text repeated.
        quality['subtitles'] = aligned.check_subtitles(value, 'zh',
            clips['repeated2']['duration_ms'], vtt=True, quality_checker=check_subtitle_text)
        for label, clip, request in [('python-threshold-one', 'repeated2', rows['high_threshold']),
                                      ('python-natural-silence', 'silence', rows['silence'])]:
            before = self.token_counters(label+'-before')
            value = self.sdk(label, clip, request, python=True)
            check_empty(value); check_empty(read(self.out/(label+'.wire')))
            require(self.token_counters(label+'-after') == before, 'silent VAD executed model')
        before = self.token_counters('chunk-count-before')
        status, raw = self.multipart('chunk-count-limit', fields,
                                     Path(clips['repeated4']['path']).read_bytes())
        require(status == 400 and json.loads(raw)['error']['code'] == 'vad_chunk_limit',
                'chunk overflow silently dropped speech')
        require(self.token_counters('chunk-count-after') == before, 'chunk refusal ran ASR')
        before = self.token_counters('output-limit-before')
        status, raw = self.multipart('shared-output-limit', fields,
                                     Path(clips['repeated3']['path']).read_bytes())
        require(status == 502 and json.loads(raw)['error']['code'] == 'asr_output_limit'
                and 'text' not in json.loads(raw), 'partial success or wrong output limit')
        after = self.token_counters('output-limit-after')
        require(after['generation_tokens'] - before['generation_tokens'] == OUTPUT_BUDGET,
                'output limit did not count all prior clips')
        before = self.drained('cancel-before')
        self.multipart('cancel-multiclip', fields+[('stream', 'true')],
                        Path(clips['repeated2']['path']).read_bytes(), cancel=True)
        after = self.drained('cancel-after')
        require(after['q4t_requests_aborted_total'] == before['q4t_requests_aborted_total']+1,
                'multiclip cancellation not observed')
        self.core('core-after-vad')
        state = self.states('final')
        require(all(v['state'] == 'ready' and v['compute_failures'] == 0 for v in state.values())
                and state['vad']['load_failures'] == 1, 'same-PID auxiliary recovery incomplete')
        require(self.counts['chats'] == 12, 'attempt accounting differs')
        save(self.out/'quality.json', quality)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='mode', required=True)
    ready = commands.add_parser('prepare')
    ready.add_argument('--binary', type=Path, required=True)
    ready.add_argument('--output', type=Path, required=True)
    ready.add_argument('--port', type=int, default=18236)
    run = commands.add_parser('run')
    run.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare': prepare(args)
    else:
        with asr.base.lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()
