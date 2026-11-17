"""One start/seven creates: real segment fields, SDKs and silent-audio score."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
import wave
import zlib

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_asr import alignment_http_acceptance as aligned
from tools.native_asr import transcription_acceptance as asr
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, chats=7, controls=2000, seconds=600)
REMAINING_LIMITS = dict(LIMITS, chats=2, seconds=300)


def requests():
    return dict(chat=asr.requests()['chat'], plain=asr.requests()['zh'],
        segment=dict(model=asr.MODEL, response_format='verbose_json'),
        combined=dict(model=asr.MODEL, response_format='verbose_json',
                      timestamp_granularities=['word', 'segment'], language='zh'),
        explicit=dict(model=asr.MODEL, response_format='verbose_json',
                      timestamp_granularities=['segment'], temperature=.2),
        words=aligned.requests()['words'])


def prepare(args):
    aligned.prepare(args)
    out = args.output.resolve()
    plan = read(out/'plan.json')
    silence = out/'silence.wav'
    with wave.open(str(silence), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(bytes(16384 * 2))
    plan['clips']['silence'] = dict(path=str(silence), sha256=sha(silence),
                                    duration_ms=1024)
    plan.update(limits=LIMITS, requests=requests(), scope=__doc__,
        planned=dict(starts=1, creates=7, segment_outputs=4,
                     ordinary_asr=1, word_only=1, core_chats=1),
        mutations=['create private readonly model links and silent WAV only',
                   'no default replacement, model restart or external service action'],
        quality=dict(zh_exact=True, en_word_error_rate_max=.05,
            segment_fields='finite actual token IDs, raw logprob, zlib ratio and temperature',
            silence='empty transcript and actual trained None posterior > 0.5',
            speech='actual trained None posterior < 0.5',
            same_zh_posterior='forced output language must not bias classifier',
            plain_vs_segment='same greedy Chinese token bytes and mean logprob',
            limitations='No full-language, diarization, VAD chunking or human timing claim'))
    if args.reuse_completed is not None:
        reused = review_stored(args.reuse_completed, verify_identity=True)
        require(plan['binary_sha256'] == reused['binary_sha256'],
                'supplement must retain the original runtime')
        plan['segment_reuse'] = reused
        plan['limits'] = REMAINING_LIMITS
        plan['planned'] = dict(starts=1, creates=2, reused_creates=5,
                               word_only=1, core_chats=1)
    save(out/'plan.json', plan)


def check_segment(value, duration_ms, *, include_words=False, empty=False):
    require(value['duration'] == duration_ms / 1000, 'actual duration differs')
    require(value['usage']['type'] == 'duration' and
            value['usage']['seconds'] == duration_ms / 1000, 'duration usage')
    require(isinstance(value.get('segments'), list), 'missing segment array')
    require(bool(value.get('words')) == include_words, 'word selection differs')
    if empty:
        require(value['text'] == '' and value['segments'] == [],
                'silent audio invented text or segment')
        return dict(empty=True)
    require(len(value['segments']) == 1, 'one actual decoded clip expected')
    row = value['segments'][0]
    require(row['text'] == value['text'] and row['text'], 'segment text differs')
    for name in ('start', 'end', 'avg_logprob', 'temperature',
                 'compression_ratio', 'no_speech_prob'):
        require(type(row[name]) in (int, float) and math.isfinite(row[name]),
                'nonfinite segment field: '+name)
    require(row['id'] == 0 and row['seek'] == 0, 'actual clip origin differs')
    require(0 <= row['start'] <= row['end'] <= duration_ms / 1000,
            'segment timestamp range')
    require(row['avg_logprob'] <= 0 and 0 <= row['temperature'] <= 1 and
            0 <= row['no_speech_prob'] <= 1, 'segment probability range')
    require(row['tokens'] and all(type(v) is int and 0 <= v < 151936
                                  for v in row['tokens']), 'actual token IDs')
    text = value['text'].encode()
    ratio = len(text) / len(zlib.compress(text, level=6))
    require(abs(row['compression_ratio'] - ratio) < 1e-12, 'actual zlib ratio')
    if include_words:
        require(row['start'] == value['words'][0]['start'] and
                row['end'] == value['words'][-1]['end'], 'actual word edges')
    return dict(tokens=len(row['tokens']), avg_logprob=row['avg_logprob'],
                temperature=row['temperature'], no_speech_prob=row['no_speech_prob'],
                compression_ratio=ratio, start=row['start'], end=row['end'])


def token_bytes(tokens, vocab):
    visible = list(range(33, 127)) + list(range(161, 173)) + list(range(174, 256))
    mapping = {chr(value): value for value in visible}
    for count, value in enumerate(v for v in range(256) if v not in visible):
        mapping[chr(256 + count)] = value
    inverse = {value: key for key, value in vocab.items()}
    return [bytes(mapping[c] for c in inverse[token]) for token in tokens]


def read_vocab(plan):
    path = Path(plan['asr_directory'])/'vocab.json'
    expected = plan['asr_assets']['vocab.json']
    require(path.stat().st_size == expected['bytes'] <= 3 * 1024**2 and
            sha(path) == expected['sha256'], 'fixed ASR vocabulary drift')
    return json.loads(path.read_text())


def review_stored(source, *, verify_identity=False):
    source = Path(source).resolve(strict=True)
    require(source.is_relative_to(aligned.WORK), 'segment evidence outside workdir')
    plan = read(source/'plan.json')
    if verify_identity:
        outcome, lease = read(source/'result.json'), read(source/'lease/exit.json')
        require(outcome['status'] == 'FAILED' and outcome['counts']['starts'] == 1
            and outcome['counts']['chats'] == 5 and outcome['default_unchanged']
            and 'metadata size limit' in outcome['error'] and
            lease['status'] == 'COMMAND_FAILED', 'unexpected reuse predecessor')
        cleanup = read(source/'cleanup.json')
        require(cleanup['model_exit'] == 0 and not cleanup['remaining'] and
                cleanup['ports_released'] and cleanup['lease_continuously_held'],
                'original cleanup incomplete')
        for name, digest in plan['sources'].items():
            if name != 'tools/native_asr/segment_http_acceptance.py':
                require(sha(ROOT/name) == digest, 'reused source drift: '+name)
        require(sha(plan['binary']) == plan['binary_sha256'], 'reused binary drift')
    quality = {}
    for label, language, words, empty in [
        ('python-default-segment-zh', 'zh', False, False),
        ('js-combined-forced-zh', 'zh', True, False),
        ('js-explicit-segment-en', 'en', False, False),
        ('python-silent-segment', 'silence', False, True)]:
        wire = read(source/(label+'.wire'))
        sdk = read(source/(label+'.sdk.json'))
        require(sdk['ok'] and len(sdk['exchanges']) == 1, 'original SDK failed or retried')
        ms = plan['clips'][language]['duration_ms']
        quality[label] = check_segment(wire, ms, include_words=words, empty=empty)
        check_segment(sdk['result'], ms, include_words=words, empty=empty)
        if not empty:
            quality[label]['text_quality'] = asr.quality(wire['text'], language)
            require(wire['segments'][0]['no_speech_prob'] < .5, 'speech labelled silent')
    baseline = read(source/'python-plain-zh.wire')
    baseline_sdk = read(source/'python-plain-zh.sdk.json')
    require(baseline_sdk['ok'] and len(baseline_sdk['exchanges']) == 1,
            'ordinary original SDK failed or retried')
    asr.quality(baseline['text'], 'zh')
    asr.usage(baseline)
    asr.usage(baseline_sdk['result'], sdk=True)
    default = read(source/'python-default-segment-zh.wire')['segments'][0]
    combined = read(source/'js-combined-forced-zh.wire')['segments'][0]
    explicit = read(source/'js-explicit-segment-en.wire')['segments'][0]
    require(explicit['temperature'] == .2, 'explicit sampling temperature ignored')
    require(default['temperature'] == 0,
            'Chinese fallback prevents comparison to ordinary greedy logs')
    actual_bytes = token_bytes(default['tokens'], read_vocab(plan))
    require(actual_bytes == [bytes(row['bytes']) for row in baseline['logprobs']],
            'segment IDs differ from actual ordinary selected token bytes')
    mean = sum(row['logprob'] for row in baseline['logprobs']) / len(baseline['logprobs'])
    require(abs(default['avg_logprob'] - mean) < 1e-10, 'actual mean logprob differs')
    require(default['no_speech_prob'] == combined['no_speech_prob'],
            'output language biased no-speech scoring')
    scores = re.findall(r'\[asr-no-speech\] request=(\S+) token=(\d+) '
        r'raw_logprob=(\S+) probability=(\S+) input=(\d+)',
        (source/'model.log').read_text())
    require(len(scores) == 4, 'actual scoring observations missing')
    values = [dict(request=r, token=int(t), raw_logprob=float(lp),
                   probability=float(p), input=int(n)) for r,t,lp,p,n in scores]
    require(all(v['token'] == 2240 and v['input'] > 0 and
                math.isfinite(v['raw_logprob']) and
                abs(math.exp(v['raw_logprob']) - v['probability']) < 1e-14
                for v in values), 'posterior is not actual full-head probability')
    require(values[-1]['probability'] > .5, 'silent label not dominant')
    require(values[0]['probability'] == default['no_speech_prob'],
            'response posterior differs from scored probability')
    quality.update(label_observations=values, greedy_mean_logprob=mean)
    files = [p for p in source.iterdir() if p.is_file() and
             (p.suffix in ('.json', '.wire', '.log') or p.name == 'model.log')]
    files += [source/'lease/exit.json', source/'lease/observations.jsonl']
    return dict(directory=str(source), binary_sha256=plan['binary_sha256'],
        status='PASS_REVIEWED_COMPLETED_FIVE_REQUESTS_ORIGINAL_RUN_STAYS_FAILED',
        observations={str(p.relative_to(source)): sha(p) for p in files if p.is_file()},
        quality=quality)


class Stage(aligned.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_NATIVE_SEGMENT_STATISTICS_SDK_SILENCE'

    def __init__(self, plan):
        if plan.get('segment_reuse'):
            self.limits = REMAINING_LIMITS
            self.success_status = 'PASS_NATIVE_SEGMENTS_WITH_REUSED_FIVE_REQUESTS'
            require(plan['segment_reuse'] == review_stored(
                plan['segment_reuse']['directory'], verify_identity=True),
                'original completed observations drift')
        super().__init__(plan)

    def start(self):
        super().start()
        assets = self.out/'aligner-assets'
        os.rename(assets/'model.safetensors.withheld', assets/'model.safetensors')
        caps = read(self.out/'capabilities.json')['transcriptions']
        require(caps['alignment']['timestamp_granularities'] == ['word', 'segment']
            and caps['segments']['adaptive_attempts_max'] == 6,
            'native segment implementation missing')

    def business(self):
        rows = self.plan['requests']
        if self.plan.get('segment_reuse'):
            quality = self.plan['segment_reuse']['quality']
        else:
            self.sdk('python-plain-zh', 'zh', rows['plain'], python=True)
            self.sdk('python-default-segment-zh', 'zh', rows['segment'], python=True)
            self.sdk('js-combined-forced-zh', 'zh', rows['combined'])
            self.sdk('js-explicit-segment-en', 'en', rows['explicit'])
            self.sdk('python-silent-segment', 'silence', rows['segment'], python=True)
            quality = review_stored(self.out)['quality']
        words = self.sdk('python-word-only-protection', 'zh', rows['words'], python=True)
        aligned.check_words(words, self.plan['timing_regression'],
                            self.plan['clips']['zh']['duration_ms'])
        self.core('core-after-segments')
        alignment, transcription = self.health('final')
        require(alignment['state'] == transcription['state'] == 'ready' and
                alignment['compute_failures'] == transcription['compute_failures'] == 0,
                'segment execution poisoned model health')
        save(self.out/'quality.json', quality)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    ready = sub.add_parser('prepare')
    ready.add_argument('--binary', type=Path, required=True)
    ready.add_argument('--output', type=Path, required=True)
    ready.add_argument('--port', type=int, default=18232)
    ready.add_argument('--reuse-completed', type=Path)
    run = sub.add_parser('run')
    run.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        with asr.base.lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()
