"""Native word timestamps/subtitles, real SDKs and scoped asset recovery."""
import argparse
import html
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import time
import wave

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_asr import transcription_acceptance as asr
from tools.serve_service.package import read, require, save, sha

WORK = ROOT / '.q4t-work/serve-audio-aligner-20261117'
LIMITS = dict(starts=1, chats=7, controls=2000, seconds=600)


def requests():
    return dict(chat=asr.requests()['chat'], plain=asr.requests()['zh'],
        words=dict(model=asr.MODEL, response_format='verbose_json',
                   timestamp_granularities=['word']),
        srt=dict(model=asr.MODEL, response_format='srt'),
        vtt=dict(model=asr.MODEL, response_format='vtt'))


def prepare(args):
    asr.base.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    out = args.output.resolve()
    plan = read(out/'plan.json')
    plan['python_sdk'] = asr.python_identity()
    for path in (ROOT/'tools/native_asr').glob('*'):
        if path.is_file():
            plan['sources'][str(path.relative_to(ROOT))] = sha(path)
    for kind, work in [('asr', asr.WORK), ('aligner', WORK)]:
        acquired = {row['name']: row for row in
                    read(work/'asset-acquisition-01.json')['files']}
        plan[kind+'_assets'] = {name: {k: acquired[name][k]
            for k in ('bytes', 'sha256')} for name in asr.FILES}
        plan[kind+'_directory'] = str(work/'assets-01')
    plan['clips'] = {}
    for language in asr.EXPECTED:
        path = asr.WORK/'real-audio-01'/('asr_'+language+'.wav')
        with wave.open(str(path), 'rb') as audio:
            duration = audio.getnframes() / audio.getframerate()
        plan['clips'][language] = dict(path=str(path), sha256=sha(path),
                                       duration_ms=math.floor(duration * 1000))
    plan['expected'] = asr.EXPECTED
    # Reuse the already retained actual Chinese classification/reference.
    # This is integration consistency, not human-labelled timing accuracy.
    prior = WORK/'compute-gpu-01/run'
    native, reference = [prior/k/'case-0/classes.u32'
                         for k in ('native', 'reference')]
    require(native.read_bytes() == reference.read_bytes(),
            'Chinese retained reference/native classification differs')
    raw = native.read_bytes()
    values = list(struct.unpack('<'+'I'*(len(raw)//4), raw))
    require(values == sorted(values) and len(values) == 26,
            'retained Chinese slot fixture drift')
    plan['timing_regression'] = dict(
        kind='prior real native/official Chinese outputs, not ground truth',
        files={str(p): sha(p) for p in (native, reference)},
        words=read(WORK/'alignment-cases-01.json')['cases'][0]['words'],
        milliseconds=[v * 80 for v in values])
    plan['planned'] = dict(starts=1, creates=7, rejected_alignment=1,
        ordinary_asr_during_failure=1, aligned_transcriptions=3, core_chats=2)
    plan['quality'] = dict(zh_exact=True, en_word_error_rate_max=0.05,
        word_times='actual Chinese reference regression, monotonic and bounded',
        subtitle_times='positive, monotonic, bounded by real audio duration',
        limits='No human timing-accuracy, full-language or segment claim')
    plan['mutations'] = [
        'readonly hardlinks into private asset directories only',
        'withhold and reveal only the private alignment weight link',
        'no model restart, default replacement or external service action']
    save(out/'plan.json', plan)


def check_words(value, oracle, duration_ms):
    asr.quality(value['text'], 'zh')
    require(value['language'] == 'Chinese', 'detected language changed')
    require(value['duration'] == duration_ms / 1000, 'actual duration missing')
    require(value.get('segments') in (None, []), 'invented segment statistics')
    words = value['words']
    require([v['word'] for v in words] == oracle['words'], 'word order differs')
    actual, previous = [], 0
    for word in words:
        start, end = word['start'], word['end']
        require(all(isinstance(v, (int, float)) and math.isfinite(v)
                    for v in (start, end)), 'nonfinite word timestamp')
        require(previous <= start <= end <= duration_ms / 1000,
                'word range/nonmonotonic timestamp')
        actual += [round(start * 1000), round(end * 1000)]
        previous = end
    require(actual == oracle['milliseconds'], 'actual Chinese timing regression')
    require(value['usage']['type'] == 'duration' and
            value['usage']['seconds'] == duration_ms / 1000,
            'actual duration usage differs')
    return dict(words=len(words), milliseconds=actual, text=value['text'],
                human_timing_accuracy_assessed=False)


def check_subtitles(text, language, duration_ms, *, vtt, quality_checker=None):
    require(isinstance(text, str), 'SDK did not return subtitle text')
    if vtt:
        require(text.startswith('WEBVTT\n\n'), 'missing WebVTT header')
        text = text[len('WEBVTT\n\n'):]
    stamp = r'(\d{2,}):(\d{2}):(\d{2})' + (r'\.' if vtt else ',') + r'(\d{3})'
    pattern = re.compile(stamp + ' --> ' + stamp)
    cues, previous = [], 0
    for i, block in enumerate(text.strip().split('\n\n'), 1):
        lines = block.splitlines()
        require(len(lines) >= 3 and lines[0] == str(i), 'invalid cue identity')
        match = pattern.fullmatch(lines[1])
        require(match is not None, 'invalid subtitle timestamp syntax')
        fields = list(map(int, match.groups()))
        require(all(fields[i] < 60 for i in (1, 2, 5, 6)), 'invalid clock range')
        times = [(fields[i]*3600 + fields[i+1]*60 + fields[i+2])*1000 + fields[i+3]
                 for i in (0, 4)]
        require(previous <= times[0] < times[1] <= duration_ms,
                'subtitle timestamps outside actual audio or overlapping')
        previous = times[1]
        cues.append(dict(start_ms=times[0], end_ms=times[1],
                         text=html.unescape('\n'.join(lines[2:]))))
    # Cues retain original punctuation/spacing; don't invent separators.
    quality = (quality_checker or asr.quality)(''.join(c['text'] for c in cues), language)
    return dict(cues=cues, quality=quality, human_timing_accuracy_assessed=False)


class Stage(asr.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_NATIVE_WORD_ALIGNMENT_SUBTITLES_SCOPED_RECOVERY'

    def additional_options(self):
        return []

    def __init__(self, plan):
        super().__init__(plan)
        for name, digest in plan['timing_regression']['files'].items():
            require(sha(name) == digest, 'retained timing reference drift')

    def start(self):
        for port in (self.plan['port'], self.plan['operations_port']):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', port))
        for kind in ('asr', 'aligner'):
            assets = self.out/(kind+'-assets')
            assets.mkdir(mode=0o700)
            for name, record in self.plan[kind+'_assets'].items():
                source = Path(self.plan[kind+'_directory'])/name
                require(source.stat().st_size == record['bytes'] and
                        sha(source) == record['sha256'], kind+' asset drift')
                withheld = kind == 'aligner' and name == 'model.safetensors'
                os.link(source, assets/(name+'.withheld' if withheld else name))
        args = [self.plan['binary'], 'serve', '--model-dir', self.plan['model'],
            '--host', '127.0.0.1', '--port', str(self.plan['port']),
            '--operations-port', str(self.plan['operations_port']), '--mtp',
            '--max-seq', '1', '--max-len', '8192', '--max-prefill', '2048',
            '--max-tokens', '64', '--max-inflight-requests', '8',
            '--max-queued-requests', '2', '--asr-model-dir',
            str(self.out/'asr-assets'), '--asr-max-output-tokens', '128',
            '--aligner-model-dir', str(self.out/'aligner-assets')]
        args += self.additional_options()
        save(self.out/'model-command.json', args)
        self.budget('starts')
        with (self.out/'model.log').open('xb', buffering=0) as log:
            self.model = subprocess.Popen(args, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        self.model_identity = asr.base.lifecycle.guard.processes()[self.model.pid]
        self.history[self.model.pid] = self.model_identity
        save(self.out/'model-identity.json', dict(pid=self.model.pid, **self.model_identity))
        end = time.monotonic() + 180
        while time.monotonic() < end:
            self.same_model()
            try:
                if self.http('/readyz', timeout=1)[0] == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.1)
        else:
            raise TimeoutError('model readiness')
        for name in ('capabilities', 'config', 'models'):
            status, raw = self.http('/v1/'+name, label='initial-'+name)
            require(status == 200, name+' unavailable')
            save(self.out/(name+'.json'), json.loads(raw))
        caps = read(self.out/'capabilities.json')
        self.client.protocol_sha256 = caps['protocol_source_sha256']
        require(caps['transcriptions']['alignment']['configured'],
                'configured alignment absent from capabilities')

    def health(self, label):
        status, _ = self.http('/readyz', label=label+'-ready')
        require(status == 200, 'alignment poisoned core readiness')
        status, raw = self.http('/v1/config', label=label+'-config')
        require(status == 200, 'private configuration unavailable')
        value = json.loads(raw)
        self.same_model()
        return value['native_alignment_health'], value['native_asr_health']

    def business(self):
        zh = Path(self.plan['clips']['zh']['path']).read_bytes()
        fields = [('model', asr.MODEL), ('response_format', 'verbose_json'),
                  ('timestamp_granularities[]', 'word')]
        status, raw = self.multipart('missing-alignment', fields, zh)
        require(status == 503 and json.loads(raw)['error']['code'] ==
                'alignment_assets_invalid', 'alignment asset error not classified')
        failed_at = time.monotonic()
        alignment, transcription = self.health('missing')
        require(alignment['state'] == 'degraded' and
                alignment['load_failures'] == 1 and
                transcription['state'] == 'ready', 'failure domain not isolated')
        self.drained('missing-drain')
        value = self.sdk('python-plain-during-failure', 'zh',
                         self.plan['requests']['plain'], python=True)
        save(self.out/'ordinary-asr-quality.json', asr.quality(value['text'], 'zh'))
        asr.usage(value, sdk=True)
        self.core('core-during-alignment-failure')
        assets = self.out/'aligner-assets'
        os.rename(assets/'model.safetensors.withheld', assets/'model.safetensors')
        time.sleep(max(0, 5.1 - (time.monotonic() - failed_at)))
        value = self.sdk('python-zh-words', 'zh', self.plan['requests']['words'],
                         python=True)
        save(self.out/'words-quality.json', check_words(value,
             self.plan['timing_regression'], self.plan['clips']['zh']['duration_ms']))
        check_words(read(self.out/'python-zh-words.wire'),
                    self.plan['timing_regression'], self.plan['clips']['zh']['duration_ms'])
        for label, language, fmt in [('js-en-srt', 'en', 'srt'),
                                      ('python-zh-vtt', 'zh', 'vtt')]:
            value = self.sdk(label, language, self.plan['requests'][fmt],
                             python=language == 'zh')
            require(value == (self.out/(label+'.wire')).read_text(),
                    'SDK/wire subtitle mismatch')
            save(self.out/(label+'-quality.json'), check_subtitles(value, language,
                self.plan['clips'][language]['duration_ms'], vtt=fmt == 'vtt'))
        self.core('core-after-alignment')
        alignment, transcription = self.health('final')
        require(alignment['state'] == transcription['state'] == 'ready' and
                alignment['load_failures'] == 1 and
                alignment['compute_failures'] == transcription['compute_failures'] == 0,
                'alignment request-driven recovery incomplete')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    ready = sub.add_parser('prepare')
    ready.add_argument('--binary', type=Path, required=True)
    ready.add_argument('--output', type=Path, required=True)
    ready.add_argument('--port', type=int, default=18228)
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
