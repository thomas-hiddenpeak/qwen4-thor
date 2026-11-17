"""One start/12 creates: actual mixed speech, shared budgets and scoped recovery."""
import argparse
import array
import http.client
import itertools
import json
import math
import os
from pathlib import Path
import re
import socket
import sys
import time
import wave

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_speaker import http_acceptance as speaker
from tools.native_asr import transcription_acceptance as asr
from tools.serve_service.package import read, require, save, sha

WORK = ROOT / '.q4t-work/serve-overlap-20261118'
LIMITS = dict(starts=1, chats=12, controls=2000, seconds=900)
REMAINING_LIMITS = dict(LIMITS, chats=8)
WEIGHT_SHA = '5f5cbbd10eae7be4e56b752cbc0d27f02305dd0e5cdc3d9c115b51881ca52299'
EXPECTED = [
    'The child almost hurt the small dog.',
    'We are sure that one war is enough.',
]
QUALITY = dict(per_voice_word_error_rate_max=.125, overlap_seconds_min=.3,
               timestamp_slack_samples=6080)


def requests():
    return dict(speaker.requests(), ordinary=asr.requests()['zh'])


def reuse_first_run(source, *, reference_fix=False):
    source = Path(source).resolve(strict=True)
    require(source.is_relative_to(WORK), 'prior evidence outside overlap workdir')
    plan, result = read(source/'plan.json'), read(source/'result.json')
    require(result['status'] == 'FAILED' and result['default_unchanged'] and
            result['counts']['starts'] == 1 and result['counts']['chats'] == 4 and
            'RuntimeError: a real source sentence was lost, duplicated or mistranscribed' in result['error'],
            'unexpected predecessor failure')
    require(read(source/'lease/exit.json')['status'] == 'COMMAND_FAILED' and
            read(source/'cleanup.json')['remaining'] == [] and
            read(source/'cleanup.json')['ports_released'], 'invalid predecessor cleanup')
    changed = {}
    reference_paths = {'src/server/chat_speaker_turns.cpp',
                       'src/server/separator_service.cpp'}
    for name, digest in plan['sources'].items():
        if name in ['tools/native_separator/http_acceptance.py',
                    'tools/native_separator/test_http_acceptance.py']:
            continue
        actual = sha(ROOT/name)
        if actual != digest:
            require(reference_fix and name in reference_paths,
                    'reused dependency changed: '+name)
            changed[name] = dict(before=digest, after=actual)
    if reference_fix:
        require(set(changed) == reference_paths, 'unexpected reference fix scope')
    original = WORK/'runtime-build-02/q4t-retained' if reference_fix else Path(plan['binary'])
    require(sha(original) == plan['binary_sha256'], 'original engine evidence changed')
    require(all(sha(path) == digest for path, digest in plan['protected'].items()),
            'reused default changed')
    for name in ['core-during-separator-failure.wire']:
        value = read(source/name)
        require(value['choices'][0]['message']['content'].strip() == '42', 'core protection missing')
    ordinary = read(source/'python-ordinary-during-failure.wire')
    asr.quality(ordinary['text'], 'zh'); asr.usage(ordinary)
    value = read(source/'python-mixed.wire')
    mixed = check_result(value, plan['clips']['mixed'], enforce_quality=False)
    require(not mixed['quality_passed'], 'original quality failure disappeared')
    return dict(directory=str(source), binary_sha256=sha(plan['binary']),
        original_binary_sha256=plan['binary_sha256'],
        kind='reference_bandwidth_fix' if reference_fix else 'unchanged_engine',
        reference_only_patches=changed,
        mixed=mixed, original_quality_status='FAILED_UNCHANGED_GATE',
        creates_reused=4, records={name:sha(source/name) for name in [
            'plan.json','result.json','cleanup.json','lease/exit.json',
            'missing-config.wire','missing-separator.wire',
            'core-during-separator-failure.wire',
            'python-ordinary-during-failure.wire','python-mixed.wire']})


def prepare(args):
    require(not (args.continue_from and args.reference_fix_from), 'one continuation only')
    continuation = (reuse_first_run(args.reference_fix_from, reference_fix=True)
                    if args.reference_fix_from else
                    reuse_first_run(args.continue_from) if args.continue_from else None)
    speaker.prepare(args)
    out = args.output.resolve()
    plan = read(out / 'plan.json')
    voices = []
    for name in ['spk1_snt1.wav', 'spk2_snt1.wav']:
        with wave.open(plan['clips'][name]['path'], 'rb') as audio:
            raw = array.array('h', audio.readframes(audio.getnframes()))
            if sys.byteorder != 'little': raw.byteswap()
            voices.append(raw)
    for name, duration, count in [('mixed', 4, 2), ('single', 4, 1),
                                  ('maximum', 30, 2), ('overlimit', 31, 0)]:
        values = [0] * (16000 * duration)
        utterances = []
        for voice, raw in enumerate(voices[:count]):
            start = 3200
            for i, value in enumerate(raw): values[start + i] += round(value * .5)
            utterances.append(dict(begin_sample=start, end_sample=start + len(raw),
                voice=str(voice), expected_text=EXPECTED[voice]))
        require(all(-32768 <= v <= 32767 for v in values), 'fixture clips waveform')
        raw = array.array('h', values)
        if sys.byteorder != 'little': raw.byteswap()
        path = out / (name + '.wav')
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
            audio.writeframes(raw.tobytes())
        plan['clips'][name] = dict(path=str(path), sha256=sha(path),
            samples=len(values), utterances=utterances,
            construction='half-gain integer PCM sum, 3200-sample prefix, zero tail')
    assets = WORK / 'native-assets-01'
    plan['separator_directory'] = str(assets)
    plan['separator_assets'] = {n: dict(bytes=(assets/n).stat().st_size,
        sha256=sha(assets/n)) for n in ['config.json', 'model.safetensors']}
    require(plan['separator_assets']['model.safetensors']['sha256'] == WEIGHT_SHA,
            'separator fixed asset identity')
    for path in (ROOT / 'tools/native_separator').glob('*'):
        if path.is_file(): plan['sources'][str(path.relative_to(ROOT))] = sha(path)
    plan.update(limits=LIMITS, requests=requests(), scope=__doc__,
        command=['/usr/bin/python3', '-B', str(Path(__file__).resolve()), 'run',
                 '--plan', str(out / 'plan.json')],
        planned=dict(starts=1, creates=12, rejected=2, cancelled=1,
                     mixed_transcriptions=4, single_voice=1, silence=1,
                     ordinary_asr=1, core_chats=2),
        quality=dict(QUALITY, acceptance='both real source sentences independently preserved; '
            'actual overlapping times, single-voice/silence cannot create a second person',
            numerical_reuse='same graph/F32 mapping at <=8 seconds; expanded 30s input '
            'exercises real runtime capacity, not a renewed full numerical matrix'),
        mutations=['private readonly fixed asset hardlinks; reveal withheld separator config',
            'sum fixed source PCM into owned mixed/maximum/single WAVs',
            'close owned socket after observing real execution progress',
            'no primary restart, default change or external service intervention'])
    if continuation:
        require(plan['binary_sha256'] == continuation['binary_sha256'],
                'remaining run must keep the same engine')
        plan.update(overlap_continuation=continuation, limits=REMAINING_LIMITS,
            planned=dict(starts=1, creates=8, reused_creates=4, rejected=1,
                         cancelled=1, mixed_transcriptions=3, single_voice=1,
                         silence=1, core_chats=1))
    save(out / 'plan.json', plan)


def check_result(value, clip, names=None, *, enforce_quality=True):
    require(value.get('task') == 'transcribe' and
            value.get('duration') == clip['samples']/16000, 'actual duration/task')
    rows, sources, usage = value['segments'], clip['utterances'], value['usage']
    require(type(rows) is list and type(value['text']) is str, 'transcript shape')
    keys = ['input_tokens', 'output_tokens', 'total_tokens']
    require(usage['type'] == 'tokens' and all(type(usage[k]) is int and
        0 <= usage[k] < 2**53 for k in keys), 'actual token usage')
    require(usage['input_tokens'] + usage['output_tokens'] == usage['total_tokens'],
            'actual token sum')
    details = usage['input_token_details']
    require(all(type(details[k]) is int and details[k] >= 0 for k in
                ['audio_tokens', 'text_tokens']) and
            details['audio_tokens'] + details['text_tokens'] == usage['input_tokens'],
            'actual audio/text token sum')
    if not sources:
        require(not rows and value['text'] == '' and all(usage[k] == 0 for k in keys),
                'silence fabricated voice/text/work')
        return dict(silence=True, quality_passed=True)
    require(rows and value['text'].strip() and all(usage[k] > 0 for k in keys) and
            details['audio_tokens'] > 0, 'missing real transcript/usage')
    grouped, previous = {}, 0
    for index, row in enumerate(rows):
        require(row.get('id') == str(index) and row.get('type') == 'transcript.text.segment',
                'segment ID/type')
        begin, end = row['start'], row['end']
        require(all(type(v) in (int, float) and math.isfinite(v) for v in [begin, end])
                and previous <= begin <= end <= clip['samples']/16000,
                'nonfinite or unordered original time')
        previous = begin
        require(type(row['speaker']) is str and row['speaker'] and
                type(row['text']) is str and row['text'].strip(), 'missing voice/text')
        grouped.setdefault(row['speaker'], []).append(row)
    require(len(grouped) == len(sources), 'lost or invented speaker')
    labels = list(grouped)
    scores = []
    for assignment in itertools.permutations(labels):
        errors = [asr.word_error_rate(' '.join(r['text'] for r in grouped[label]),
                                     source['expected_text'])
                  for label, source in zip(assignment, sources)]
        scores.append((sum(errors), assignment, errors))
    _, assignment, errors = min(scores)
    quality_passed = all(v <= QUALITY['per_voice_word_error_rate_max'] for v in errors)
    if enforce_quality:
        require(quality_passed,
                'a real source sentence was lost, duplicated or mistranscribed')
    for label, source in zip(assignment, sources):
        if names is not None:
            require(label == names[source['voice']], 'known reference identity differs')
        for row in grouped[label]:
            require(row['start']*16000 >= source['begin_sample'] - 6080 and
                    row['end']*16000 <= source['end_sample'] + 6080,
                    'voice time outside its real source')
    overlap = 0
    if len(sources) == 2:
        overlap = max(min(a['end'], b['end']) - max(a['start'], b['start'])
                      for a in grouped[assignment[0]] for b in grouped[assignment[1]])
        require(overlap >= QUALITY['overlap_seconds_min'],
                'simultaneous voices were incorrectly serialized')
    compact = lambda text: re.sub(r'\s+', '', text)
    require(compact(''.join(r['text'] for r in rows)) == compact(value['text']),
            'full text differs from actual segments')
    return dict(voice_labels=list(assignment), per_voice_word_error_rate=errors,
                quality_passed=quality_passed,
                overlap_seconds=overlap, text=value['text'])


def check_stream(value, clip, *, enforce_quality=True):
    events = value['events']
    require(events and events[-1]['type'] == 'transcript.text.done' and
            sum(e['type'] == 'transcript.text.done' for e in events) == 1,
            'missing or duplicated final event')
    checked = check_result(events[-1], clip, enforce_quality=enforce_quality)
    segments, ids, delta = [], [], []
    for event in events[:-1]:
        if event['type'] == 'transcript.text.delta':
            ids.append(event['segment_id']); delta.append(event['delta'])
        elif event['type'] == 'transcript.text.segment': segments.append(event)
        else: raise RuntimeError('unexpected overlap event')
    require(''.join(delta) == events[-1]['text'] and
            ids == [s['id'] for s in segments], 'stream delta/segment identity')
    final = events[-1]['segments']
    require(len(segments) == len(final), 'stream lost a segment')
    for a, b in zip(segments, final):
        require(all(a[k] == b[k] for k in ['id', 'type', 'speaker', 'start', 'end', 'text']),
                'stream changed committed segment')
    return checked


class Stage(speaker.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_NATIVE_OVERLAP_HTTP_SDK_REFERENCES_SCOPED_RECOVERY'

    def __init__(self, plan):
        if plan.get('overlap_continuation'):
            self.limits = REMAINING_LIMITS
        super().__init__(plan)
        if plan.get('overlap_continuation'):
            require(plan['overlap_continuation'] == reuse_first_run(
                plan['overlap_continuation']['directory'], reference_fix=
                plan['overlap_continuation']['kind'] == 'reference_bandwidth_fix'),
                'reused observations changed')

    def additional_options(self):
        return super().additional_options() + [
            '--separator-model-dir', str(self.out/'separator-assets'),
            '--separator-max-audio-seconds', '30', '--separator-workspace-mib', '2048']

    def start(self):
        assets = self.out/'separator-assets'; assets.mkdir(mode=0o700)
        for name, record in self.plan['separator_assets'].items():
            source = Path(self.plan['separator_directory'])/name
            require(sha(source) == record['sha256'] and
                    source.stat().st_size == record['bytes'], 'separator asset drift')
            os.link(source, assets/(name+'.withheld' if name == 'config.json' and
                not self.plan.get('overlap_continuation') else name))
        super().start()
        assets = self.out/'speaker-assets'
        os.rename(assets/'config.json.withheld', assets/'config.json')
        require(read(self.out/'capabilities.json')['transcriptions']['separator']['configured'],
                'separator capability absent')

    def state(self, label):
        require(self.http('/readyz', label=label+'-ready')[0] == 200,
                'separator failure poisoned core readiness')
        status, raw = self.http('/v1/config', label=label+'-config')
        require(status == 200, 'separator config missing')
        self.same_model()
        return json.loads(raw)['native_separator_health']

    def cancel_during_execution(self, audio):
        before = self.drained('cancel-before')
        boundary = 'q4t-overlap-cancellation'
        fields = [('model', asr.MODEL), ('response_format', 'diarized_json'),
                  ('language', 'en')]
        raw = b''.join(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+
            key+'"\r\n\r\n'+value+'\r\n').encode() for key, value in fields)
        raw += ('--'+boundary+'\r\nContent-Disposition: form-data; name="file"; '
                'filename="voice.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode()
        raw += audio + ('\r\n--'+boundary+'--\r\n').encode()
        self.budget('chats')
        connection = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=30)
        try:
            connection.request('POST', '/v1/audio/transcriptions', body=raw,
                headers={'Content-Type': 'multipart/form-data; boundary='+boundary})
            end = time.monotonic()+30
            while time.monotonic() < end:
                status, raw = self.http('/metrics', timeout=5)
                require(status == 200, 'cancellation progress observation unavailable')
                current = asr.base.lifecycle.parse_metrics(raw)
                if current['q4t_execution_steps_total'] > before['q4t_execution_steps_total']:
                    save(self.out/'cancel-progress.json', dict(before=before, observed=current))
                    break
                time.sleep(.02)
            else: raise TimeoutError('no actual separator progress before cancellation')
            connection.sock.shutdown(socket.SHUT_RDWR)
        finally: connection.close()
        after = self.drained('cancel-after')
        require(after['q4t_requests_aborted_total'] == before['q4t_requests_aborted_total']+1,
                'in-flight cancellation not observed')

    def business(self):
        clips, rows = self.plan['clips'], self.plan['requests']
        fields = [('model', asr.MODEL), ('response_format', 'diarized_json'), ('language', 'en')]
        mixed = Path(clips['mixed']['path']).read_bytes()
        if not self.plan.get('overlap_continuation'):
            status, raw = self.multipart('missing-separator', fields, mixed)
            require(status == 503 and json.loads(raw)['error']['code'] == 'separator_assets_invalid',
                    'missing separator assets misclassified')
            failed_at = time.monotonic()
            state = self.state('missing')
            require(state['state'] == 'degraded' and state['load_failures'] == 1,
                    'separator failure not independently visible')
            self.core('core-during-separator-failure')
            ordinary = self.sdk('python-ordinary-during-failure', 'zh', rows['ordinary'], python=True)
            asr.quality(ordinary['text'], 'zh'); asr.usage(ordinary, sdk=True)
            assets = self.out/'separator-assets'
            os.rename(assets/'config.json.withheld', assets/'config.json')
            time.sleep(max(0, 5.1-(time.monotonic()-failed_at)))
        quality = {}
        if self.plan.get('overlap_continuation'):
            quality['reused-python-mixed'] = self.plan['overlap_continuation']['mixed']
        for label, clip, request, python, names in [
            ('python-mixed', 'mixed', rows['plain'], True, None),
            ('python-known', 'mixed', rows['known'], True, {'0':'first','1':'second'}),
            ('python-single', 'single', rows['plain'], True, None),
            ('js-maximum', 'maximum', rows['plain'], False, None)]:
            if self.plan.get('overlap_continuation') and label == 'python-mixed':
                continue
            value = self.sdk(label, clip, request, python=python)
            quality[label] = check_result(value, clips[clip], names, enforce_quality=False)
            check_result(read(self.out/(label+'.wire')), clips[clip], names,
                         enforce_quality=False)
            save(self.out/'quality.json', quality)
        state = self.state('recovered')
        require(state['state'] == 'ready' and state['load_failures'] ==
                (0 if self.plan.get('overlap_continuation') else 1) and
                state['compute_failures'] == 0, 'same-PID separator recovery absent')
        value = self.sdk('js-overlap-stream', 'mixed', rows['stream'])
        quality['stream'] = check_stream(value, clips['mixed'], enforce_quality=False)
        require(b'[DONE]' not in (self.out/'js-overlap-stream.wire').read_bytes(), 'Chat sentinel')
        silent = self.sdk('python-silence', 'silence', rows['plain'], python=True)
        quality['silence'] = check_result(silent, clips['silence'])
        status, raw = self.multipart('duration-capacity', fields,
                                     Path(clips['overlimit']['path']).read_bytes())
        require(status == 413 and json.loads(raw)['error']['code'] == 'file_audio_capacity',
                'capacity silently truncated or failed incorrectly')
        self.cancel_during_execution(Path(clips['maximum']['path']).read_bytes())
        self.core('core-after-overlap')
        state = self.state('final')
        require(state['state'] == 'ready' and state['compute_failures'] == 0,
                'final separator health differs')
        require(self.counts['chats'] == self.limits['chats'], 'create accounting differs')
        save(self.out/'quality.json', quality)
        accepted = all(row['quality_passed'] for row in quality.values())
        save(self.out/'business-review.json', dict(protocol_checks_passed=True,
            quality_passed=accepted, thresholds=QUALITY, original_quality_not_rewritten=True))
        require(accepted, 'frozen per-voice quality gate failed; protocol results retained')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    prepare_parser = sub.add_parser('prepare')
    prepare_parser.add_argument('--binary', type=Path, required=True)
    prepare_parser.add_argument('--output', type=Path, required=True)
    prepare_parser.add_argument('--port', type=int, default=18260)
    prepare_parser.add_argument('--continue-from', type=Path)
    prepare_parser.add_argument('--reference-fix-from', type=Path)
    run = sub.add_parser('run'); run.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare': prepare(args)
    else:
        with asr.base.lifecycle.child_subreaper(): Stage(read(args.plan)).run()


if __name__ == '__main__': main()
