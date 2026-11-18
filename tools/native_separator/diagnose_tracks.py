"""Bounded waveform-only diagnosis; never certifies HTTP quality or release."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_speaker.acceptance import defaults, digest

LIMITS = dict(gpu_model_loads=1, primary_model_starts=0, cases=4,
              http_requests=0, seconds=240, output_pcm_bytes=2*1024**2)


def prepare(args):
    out = args.output.resolve()
    out.mkdir(exist_ok=False)
    first = ROOT/'.q4t-work/serve-overlap-20261118/http-03'
    previous = json.loads((first/'plan.json').read_bytes())
    source = {}
    def read_wave(key):
        clip = previous['clips'][key]
        path = Path(clip['path'])
        assert digest(path) == clip['sha256']
        source[str(path)] = digest(path)
        with wave.open(str(path), 'rb') as audio:
            assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1,2,16000)
            return np.frombuffer(audio.readframes(audio.getnframes()), '<i2').astype(np.float64)/32768.
    single, mixed, other = [read_wave(n) for n in ['single','mixed','spk2_snt1.wav']]
    solo_other = np.zeros(64000); solo_other[3200:3200+len(other)] = other*.5
    index = np.arange(-32,33,dtype=np.float64)
    kernel = .45*np.sinc(.45*index)*np.hanning(65); kernel /= kernel.sum()
    cases = []
    for i, (label, pcm) in enumerate([('single',single), ('mixed',mixed),
                                    ('quiet_single',single*.1), ('second_solo',solo_other)]):
        samples = np.convolve(pcm,kernel,mode='full')[32:32+len(pcm)][::2].astype('<f4')
        assert len(samples)==32000
        path = out/f'case-{i}.f32'; path.write_bytes(samples.tobytes())
        cases.append(dict(label=label,samples=len(samples),pcm=str(path),pcm_sha256=digest(path)))
        source[str(path)] = digest(path)
    binary = args.binary.resolve(strict=True)
    assets = ROOT/'.q4t-work/serve-overlap-20261118/native-assets-01'
    files = [binary, assets/'model.safetensors',assets/'config.json',Path(__file__),
             ROOT/'tools/native_separator/model_replay.cpp',
             ROOT/'tools/native_speaker/acceptance.py',
             ROOT/'src/quant/lt_cache.cpp', ROOT/'src/io/json.cpp',ROOT/'src/trace/sha256.cpp',
             ROOT/'src/runtime/system_memory.cpp']
    for folder in ['include/q4t/audio','src/audio']:
        files += list((ROOT/folder).glob('separator*'))
    source.update({str(p):digest(p) for p in files})
    plan = dict(limits=LIMITS,identities=source,cases=cases,trace_stages=False,
        binary=str(binary),assets=str(assets),output=str(out),defaults=defaults(),
        command=['/usr/bin/python3','-B',str(Path(__file__).resolve()),'run','--plan',str(out/'plan.json')],
        purpose='measure actual source duplication; no changed thresholds or release acceptance',
        resampling='same fixed 65-tap 3600Hz Hann/sinc, zero extension, even decimation')
    (out/'plan.json').write_text(json.dumps(plan,indent=2)+'\n')
    print(json.dumps(dict(output=str(out),limits=LIMITS)))


def correlation(a,b):
    a,b=a-a.mean(),b-b.mean()
    return float(a@b / np.sqrt(max(float(a@a)*float(b@b),1e-30)))


def observations(plan, directory):
    result=[]
    for i,case in enumerate(plan['cases']):
        a=np.fromfile(case['pcm'],'<f4').astype(np.float64)
        b=np.fromfile(directory/f'case-{i}'/'sources.f32','<f4').reshape(2,-1).astype(np.float64)
        assert b.shape==(2,len(a)) and np.isfinite(b).all()
        windows=[]
        for start in range(0,len(a)-2000+1,1000):
            x,y=b[:,start:start+2000]
            windows.append(dict(begin_sample=start,rms=[float(np.sqrt(v@v/len(v))) for v in [x,y]],
                                cosine=correlation(x,y)))
        result.append(dict(label=case['label'],input_rms=float(np.sqrt(a@a/len(a))),
            output_rms=[float(np.sqrt(v@v/len(v))) for v in b],
            pair_cosine=correlation(*b), input_cosines=[correlation(a,v) for v in b],
            windows=windows))
    return result


def run(plan_path):
    plan=json.loads(plan_path.read_bytes());assert plan['limits']==LIMITS
    assert all(digest(p)==h for p,h in plan['identities'].items())
    assert defaults()==plan['defaults']
    lease=int(os.environ['Q4T_MANAGED_LEASE_FD']);os.fstat(lease)
    out=Path(plan['output']);started=time.monotonic();result=dict(status='FAILED')
    try:
        cmd=[plan['binary'],plan['assets'],str(plan_path),str(out/'native')]
        with (out/'native.log').open('x') as log:
            child=subprocess.run(cmd,pass_fds=(lease,),stdout=log,stderr=subprocess.STDOUT,
                                 timeout=180,cwd=ROOT)
        result['returncode']=child.returncode;assert child.returncode==0
        native=json.loads((out/'native/result.json').read_bytes())
        assert native['gpu_model_loads']==1 and native['cases']==4
        assert sum(p.stat().st_size for p in (out/'native').rglob('*.f32'))<=LIMITS['output_pcm_bytes']
        result['observations']=observations(plan,out/'native')
        result['status']='COMPLETED_DIAGNOSIS_PENDING_LEASE_RECONCILIATION'
    except Exception as error:result['error']=repr(error)
    finally:
        result.update(seconds=time.monotonic()-started,defaults_unchanged=defaults()==plan['defaults'])
        if not result['defaults_unchanged'] or result['seconds']>LIMITS['seconds']:result['status']='FAILED'
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ['status','seconds','defaults_unchanged']}))
    return 0 if result['status'].startswith('COMPLETED_') else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='mode',required=True)
    p=sub.add_parser('prepare');p.add_argument('--binary',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('run');p.add_argument('--plan',type=Path,required=True)
    args=parser.parse_args()
    if args.mode=='prepare':prepare(args)
    else:raise SystemExit(run(args.plan.resolve(strict=True)))
