"""Targeted follow-up: normalized mixed PCM, silent completion and pending checks."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_separator import http_acceptance as overlap
from tools.native_speaker import http_acceptance as speaker
from tools.native_asr import transcription_acceptance as asr
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, chats=8, controls=2000, seconds=900)


def reuse(source):
    source = source.resolve(strict=True)
    require(source.is_relative_to(overlap.WORK), 'predecessor outside overlap evidence')
    plan, result = read(source/'plan.json'), read(source/'result.json')
    require(result['status'] == 'FAILED' and result['default_unchanged'] and
            result['counts']['starts'] == 1 and result['counts']['chats'] == 7 and
            'actual SDK request failed: python-silence' in result['error'],
            'unexpected predecessor failure')
    require(read(source/'lease/exit.json')['status'] == 'COMMAND_FAILED' and
            read(source/'cleanup.json')['remaining'] == [] and
            read(source/'cleanup.json')['ports_released'], 'predecessor cleanup invalid')
    error = read(source/'python-silence.wire')['error']
    require(error['code'] == 'asr_usage_invalid', 'silence failure changed')
    retained = overlap.WORK/'output-fix-build-01/q4t-pre-fix'
    require(sha(retained) == plan['binary_sha256'], 'original engine not retained')
    allowed = {'src/server/chat_asr.cpp', 'src/server/chat_separator_execution.cpp',
               'src/audio/separator_resample.cpp', 'include/q4t/audio/separator_resample.h'}
    changed = {}
    for path, digest in plan['sources'].items():
        actual = sha(ROOT/path)
        if actual != digest:
            require(path in allowed, 'unreviewed predecessor dependency drift: '+path)
            changed[path] = dict(before=digest, after=actual)
    require(set(changed) == allowed, 'unexpected output adapter repair scope')
    require(all(sha(p) == h for p, h in plan['protected'].items()), 'default changed')
    checked = {}
    for label, clip in [('python-single','single'), ('python-second-solo','second_solo'),
                        ('python-quiet-single','quiet_single')]:
        checked[label] = overlap.check_result(read(source/(label+'.wire')), plan['clips'][clip])
    files = ['plan.json','result.json','cleanup.json','lease/exit.json','python-silence.wire',
             *(label+'.wire' for label in checked)]
    return dict(directory=str(source), records={n:sha(source/n) for n in files},
                protected_singles=checked, reviewed_changes=changed,
                original_quality_failure_retained=True,
                original_binary_sha256=plan['binary_sha256'])


def prepare(args):
    prior = reuse(args.from_directory)
    args.continue_from = args.reference_fix_from = args.track_fix_from = None
    overlap.prepare(args)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    plan.update(limits=LIMITS, scope=__doc__, output_followup=prior,
        overlap_continuation=dict(kind='output_adapter_fix'),
        command=['/usr/bin/python3','-B',str(Path(__file__).resolve()),
                 'run','--plan',str(path)],
        planned=dict(starts=1,creates=8,reused_single_voice=3,
            mixed_transcriptions=3,silence=2,rejected=1,cancelled=1,core_chats=1))
    save(path, plan)


class Stage(overlap.Stage):
    limits = LIMITS

    def __init__(self, plan):
        # Reuse the common one-process lifecycle, but verify this repair's own
        # predecessor and affected cases instead of the first-run WER receipt.
        speaker.Stage.__init__(self, plan)
        require(plan['output_followup'] == reuse(Path(plan['output_followup']['directory'])),
                'output follow-up evidence changed')

    def business(self):
        clips, rows = self.plan['clips'], self.plan['requests']
        quality = {}
        for label, clip, request, python, names in [
            ('python-known','mixed',rows['known'],True,{'0':'first','1':'second'}),
            ('js-maximum','maximum',rows['plain'],False,None)]:
            value = self.sdk(label,clip,request,python=python)
            quality[label] = overlap.check_result(value, clips[clip], names,
                                                   enforce_quality=False)
            overlap.check_result(read(self.out/(label+'.wire')), clips[clip], names,
                                  enforce_quality=False)
            save(self.out/'quality.json',quality)
        value = self.sdk('js-overlap-stream','mixed',rows['stream'])
        quality['stream'] = overlap.check_stream(value,clips['mixed'],enforce_quality=False)
        require(b'[DONE]' not in (self.out/'js-overlap-stream.wire').read_bytes(),
                'Chat sentinel in audio stream')
        value = self.sdk('python-silence','silence',rows['plain'],python=True)
        quality['silence'] = overlap.check_result(value,clips['silence'])
        value = self.sdk('js-silence-stream','silence',rows['stream'])
        quality['silence_stream'] = overlap.check_stream(value,clips['silence'])
        save(self.out/'quality.json',quality)
        fields = [('model',asr.MODEL),('response_format','diarized_json'),('language','en')]
        status, raw = self.multipart('duration-capacity',fields,
                                     Path(clips['overlimit']['path']).read_bytes())
        require(status == 413 and json.loads(raw)['error']['code'] == 'file_audio_capacity',
                'duration capacity not enforced')
        self.cancel_during_execution(Path(clips['maximum']['path']).read_bytes())
        self.core('core-after-overlap')
        state = self.state('final')
        require(state['state'] == 'ready' and state['compute_failures'] == 0 and
                state['load_failures'] == 0, 'final auxiliary health differs')
        require(self.counts['chats'] == LIMITS['chats'], 'create accounting differs')
        accepted = all(row['quality_passed'] for row in quality.values())
        save(self.out/'business-review.json',dict(protocol_checks_passed=True,
            quality_passed=accepted,thresholds=overlap.QUALITY,
            original_quality_not_rewritten=True, singles_reused=self.plan['output_followup']))
        require(accepted,'frozen per-voice quality gate failed; protocol results retained')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode',required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--binary',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--from-directory',type=Path,required=True)
    p.add_argument('--port',type=int,default=18260)
    p = sub.add_parser('run');p.add_argument('--plan',type=Path,required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':prepare(args)
    else:
        with asr.base.lifecycle.child_subreaper():Stage(read(args.plan)).run()


if __name__ == '__main__':main()
