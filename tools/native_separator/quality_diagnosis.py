"""One start/three fixed acoustic ablations; diagnosis, never release acceptance."""
import argparse
import array
import json
from pathlib import Path
import struct
import subprocess
import sys
import wave

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.native_separator import http_acceptance as overlap
from tools.native_speaker import http_acceptance as speaker
from tools.native_asr import transcription_acceptance as asr
from tools.serve_service.package import read,require,save,sha

LIMITS = dict(starts=1,chats=3,controls=1000,seconds=300)


def requests():
    return dict(acoustic=dict(model=asr.MODEL,language='en',response_format='json',
        include=['logprobs'],
        chunking_strategy=dict(type='server_vad',threshold=.5,
            prefix_padding_ms=300,silence_duration_ms=500)))


def prepare(args):
    original = overlap.WORK/'http-04/single.wav'
    previous = read(overlap.WORK/'http-04/plan.json')['clips']['single']
    require(sha(original) == previous['sha256'],'original voice source changed')
    diagnostic = overlap.WORK/'track-diagnostic-01'
    require(read(diagnostic/'lease/exit.json')['status'] == 'COMPLETED',
            'retained acoustic diagnostic was not completed')
    sources = diagnostic/'native/case-1/sources.f32'
    native = read(diagnostic/'native/result.json')
    require(native['gpu_model_loads'] == 1 and native['cases'] == 4,
            'retained native separator result changed')
    prior = read(overlap.WORK/'http-05/plan.json')
    require(sha(args.binary) == prior['binary_sha256'], 'ASR engine changed since failure')
    speaker.prepare(args)
    out = args.output.resolve();plan = read(out/'plan.json')
    with wave.open(str(original),'rb') as audio:
        require((audio.getnchannels(),audio.getsampwidth(),audio.getframerate()) ==
                (1,2,16000),'fixed original PCM format')
        samples = array.array('h',audio.readframes(audio.getnframes()))
    if sys.byteorder != 'little':samples.byteswap()
    values = array.array('f',(value/32768. for value in samples))
    if sys.byteorder != 'little':values.byteswap()
    input_path = out/'input.f32';input_path.write_bytes(values.tobytes())
    pcm = out/'pcm';pcm.mkdir()
    preprocessor = args.preprocessor.resolve(strict=True)
    cmd = [str(preprocessor),str(input_path),str(sources),str(pcm)]
    result = subprocess.run(cmd,capture_output=True,text=True,timeout=30)
    (out/'prepare.stdout').write_text(result.stdout)
    (out/'prepare.stderr').write_text(result.stderr)
    require(result.returncode == 0,'acoustic preparation failed')
    observation = json.loads(result.stdout)
    for label in ['original','limited','separated']:
        payload = (pcm/(label+'.f32')).read_bytes()
        require(len(payload) == 64000*4,'acoustic length changed')
        # IEEE float WAV transports the exact prepared F32 waveform. No PCM16
        # clipping or rounding confounds the bandwidth/separation comparison.
        encoded = (b'RIFF'+struct.pack('<I',36+len(payload))+b'WAVEfmt '+
            struct.pack('<IHHIIHH',16,3,1,16000,64000,4,32)+b'data'+
            struct.pack('<I',len(payload))+payload)
        path = out/(label+'.wav');path.write_bytes(encoded)
        plan['clips'][label] = dict(path=str(path),sha256=sha(path),samples=64000)
        plan['sources'][str(path)] = sha(path)
        plan['sources'][str(pcm/(label+'.f32'))] = sha(pcm/(label+'.f32'))
    paths = [original,sources,diagnostic/'native/result.json',diagnostic/'lease/exit.json',
             input_path,preprocessor,Path(__file__).resolve(),
             ROOT/'tools/native_separator/quality_inputs.cpp']
    paths += [p for p in (ROOT/'tools/native_separator').glob('*') if p.is_file()]
    plan['sources'].update({str(p):sha(p) for p in paths})
    plan.update(limits=LIMITS,requests=requests(),scope=__doc__,
        planned=dict(starts=1,creates=3,diagnosis_only=True),
        acoustic_preparation=dict(command=cmd,observation=observation,
            purpose='differentiate source recognition, 8k bandwidth and separation distortion',
            original_expected_text=overlap.EXPECTED[0],no_model_or_threshold_change=True),
        command=['/usr/bin/python3','-B',str(Path(__file__).resolve()),'run',
                 '--plan',str(out/'plan.json')])
    save(out/'plan.json',plan)


class Stage(speaker.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'COMPLETED_ACOUSTIC_DIAGNOSIS_NOT_RELEASE_ACCEPTANCE'

    def business(self):
        observations = {}
        for label in ['original','limited','separated']:
            result = self.sdk('python-'+label,label,self.plan['requests']['acoustic'],python=True)
            asr.usage(result,sdk=True)
            require(type(result.get('text')) is str and result['text'].strip(),
                    'acoustic diagnostic lost actual transcription')
            observations[label] = dict(text=result['text'],
                word_error_rate=asr.word_error_rate(result['text'],overlap.EXPECTED[0]),
                usage=result['usage'])
            save(self.out/'observations.json',observations)
        require(self.counts['chats'] == 3,'acoustic diagnostic budget differs')
        save(self.out/'diagnostic-review.json',dict(observations=observations,
            release_acceptance=False,original_quality_failure_retained=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode',required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--binary',type=Path,required=True)
    p.add_argument('--preprocessor',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--port',type=int,default=18260)
    p = sub.add_parser('run');p.add_argument('--plan',type=Path,required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':prepare(args)
    else:
        with asr.base.lifecycle.child_subreaper():Stage(read(args.plan)).run()


if __name__ == '__main__':main()
