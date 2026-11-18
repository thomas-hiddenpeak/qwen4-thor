"""One fixed half-chunk shift ensemble on retained mixtures, not release evidence."""
import argparse
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import wave

import numpy as np

sys.dont_write_bytecode = True
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.native_separator.acceptance import separation_quality
from tools.native_separator.http_acceptance import WORK
from tools.native_speaker.acceptance import defaults,digest

SHIFT=1000  # 125 encoder frames: exactly half a 250-frame chunk at stride 8.
LIMITS=dict(gpu_model_loads=1,primary_model_starts=0,cases=2,candidates=1,
            http_requests=0,seconds=240,output_pcm_bytes=2*1024**2)


def aligned_average(base,shifted):
    """Match arbitrary PIT channel order/sign/gain without using clean sources."""
    if base.ndim != 2 or base.shape[0] != 2 or base.shape != shifted.shape or \
            not np.isfinite(base).all() or not np.isfinite(shifted).all():
        raise ValueError('two finite, equally aligned tracks required')
    base,shifted=base.astype(np.float64),shifted.astype(np.float64)
    aa=np.sum(base*base,axis=1);bb=np.sum(shifted*shifted,axis=1)
    if min(*aa,*bb)<=1e-20:raise ValueError('active independent tracks required')
    cross=base@shifted.T
    correlation=cross/np.sqrt(aa[:,None]*bb[None,:])
    permutations=list(itertools.permutations(range(2)))
    chosen=max(permutations,key=lambda p:sum(abs(correlation[i,p[i]]) for i in range(2)))
    if any(abs(correlation[i,chosen[i]])<.9 for i in range(2)):
        raise ValueError('shifted source identity ambiguous')
    gains=np.array([cross[i,chosen[i]]/bb[chosen[i]] for i in range(2)])
    result=(.5*(base+gains[:,None]*shifted[list(chosen)])).astype('<f4')
    return result,dict(permutation=list(chosen),gains=gains.tolist(),
                       correlation=correlation.tolist())


def prepare(args):
    out=args.output.resolve();out.mkdir(exist_ok=False)
    analysis=read(WORK/'consistency-analysis-03/plan.json')
    assert analysis['limits']['cases']==2
    assert all(digest(p)==h for p,h in analysis['identities'].items())
    component=read(WORK/'component-02/plan.json');case=component['cases'][2]
    http=read(WORK/'http-04/plan.json')
    original_truth=np.fromfile(case['known_sources'],'<f4').reshape(2,-1)
    rounded_truth=[];truth_files=[WORK/'http-04/plan.json']
    for name in ['spk1_snt1.wav','spk2_snt1.wav']:
        record=http['clips'][name];path=Path(record['path'])
        assert digest(path)==record['sha256'];truth_files.append(path)
        with wave.open(str(path),'rb') as clip:
            assert (clip.getnchannels(),clip.getsampwidth(),clip.getframerate())==(1,2,16000)
            pcm=np.frombuffer(clip.readframes(clip.getnframes()),'<i2').astype(np.float64)
        raw=np.zeros(64000);raw[3200:3200+len(pcm)]=np.rint(pcm*.5)/32768.
        axis=np.arange(-32,33,dtype=np.float64)
        kernel=.45*np.sinc(.45*axis)*np.hanning(65);kernel/=kernel.sum()
        rounded_truth.append(np.convolve(raw,kernel,mode='full')[32:32+len(raw)][::2].astype('<f4'))
    truths=[original_truth,np.asarray(rounded_truth,dtype='<f4')]
    entries=[dict(label='original_component',pcm=case['pcm'],
                  baseline=str(WORK/'component-02/run/native/case-2/sources.f32')),
             dict(label='http_integer_mix',pcm=str(WORK/'track-diagnostic-01/case-1.f32'),
                  baseline=str(WORK/'track-diagnostic-01/native/case-1/sources.f32'))]
    identities=dict(analysis['identities']);cases=[]
    for i,entry in enumerate(entries):
        source=np.fromfile(entry['pcm'],'<f4');assert source.size==32000
        assert truths[i].shape==(2,32000)
        assert np.max(abs(truths[i].astype(np.float64).sum(axis=0)-source))<1e-7
        truth_path=out/f'truth-{i}.f32';truth_path.write_bytes(truths[i].tobytes())
        shifted=np.concatenate((np.zeros(SHIFT,dtype='<f4'),source))
        path=out/f'case-{i}.f32';path.write_bytes(shifted.tobytes())
        entry.update(samples=len(shifted),original_pcm=entry['pcm'],pcm=str(path),
                     pcm_sha256=digest(path),source_truth=str(truth_path))
        identities[str(path)]=digest(path);identities[entry['baseline']]=digest(entry['baseline'])
        identities[str(truth_path)]=digest(truth_path)
        cases.append(entry)
    binary=args.binary.resolve(strict=True);assets=WORK/'native-assets-01'
    files=truth_files+[binary,assets/'model.safetensors',assets/'config.json',Path(__file__).resolve(),
           ROOT/'tools/native_separator/model_replay.cpp',ROOT/'tools/native_separator/acceptance.py',
           ROOT/'tools/native_speaker/acceptance.py',ROOT/'src/quant/lt_cache.cpp',
           ROOT/'src/io/json.cpp',ROOT/'src/trace/sha256.cpp',ROOT/'src/runtime/system_memory.cpp']
    for folder in ['include/q4t/audio','src/audio']:
        files+=list((ROOT/folder).glob('separator*'))
    identities.update({str(p):digest(p) for p in files})
    plan=dict(limits=LIMITS,identities=identities,cases=cases,trace_stages=False,
        binary=str(binary),assets=str(assets),output=str(out),defaults=defaults(),
        shift_samples=SHIFT,
        source_identity='absolute waveform correlation, sign/gain fit; no clean source or text',
        fixed_candidate='equal average of original and one aligned half-chunk shifted view',
        required='both sources SI-SDR must not decrease; retain >=5dB each over mixture',
        reference='https://github.com/facebookresearch/demucs/blob/main/demucs/apply.py',
        command=['/usr/bin/python3','-B',str(Path(__file__).resolve()),'run','--plan',str(out/'plan.json')])
    save(out/'plan.json',plan);print(json.dumps(dict(limits=LIMITS,output=str(out))))


def read(path):return json.loads(Path(path).read_bytes())
def save(path,value):Path(path).write_text(json.dumps(value,indent=2)+'\n')


def run(plan_path):
    plan=read(plan_path);assert plan['limits']==LIMITS and plan['shift_samples']==SHIFT
    assert all(digest(p)==h for p,h in plan['identities'].items())
    assert defaults()==plan['defaults']
    lease=int(os.environ['Q4T_MANAGED_LEASE_FD']);os.fstat(lease)
    out=Path(plan['output']);started=time.monotonic();result=dict(status='FAILED')
    try:
        with (out/'native.log').open('x') as log:
            process=subprocess.run([plan['binary'],plan['assets'],str(plan_path),str(out/'native')],
                pass_fds=(lease,),stdout=log,stderr=subprocess.STDOUT,timeout=180,cwd=ROOT)
        assert process.returncode==0
        native=read(out/'native/result.json')
        assert native['cases']==2 and native['gpu_model_loads']==1
        assert native['boundary_checks']==8 and native['lifecycle_checks']==8
        observations=[]
        for i,case in enumerate(plan['cases']):
            truth=np.fromfile(case['source_truth'],'<f4').reshape(2,-1)
            base=np.fromfile(case['baseline'],'<f4').reshape(2,-1)
            shifted=np.fromfile(out/f'native/case-{i}/sources.f32','<f4').reshape(2,-1)[:,SHIFT:]
            candidate,alignment=aligned_average(base,shifted)
            candidate.tofile(out/f'ensemble-{i}.f32')
            before,after=separation_quality(truth,base),separation_quality(truth,candidate)
            retained=all(b>=a for a,b in zip(before['separated_si_sdr_db'],after['separated_si_sdr_db']))
            observations.append(dict(case=case['label'],alignment=alignment,before=before,after=after,
                                     both_sources_not_worse=retained))
        assert sum(p.stat().st_size for p in out.glob('ensemble-*.f32'))+\
               sum(p.stat().st_size for p in (out/'native').rglob('*.f32'))<=LIMITS['output_pcm_bytes']
        result.update(status='COMPLETED_SHIFT_DIAGNOSIS_PENDING_LEASE_RECONCILIATION',
            observations=observations,signal_candidate=all(r['both_sources_not_worse'] and r['after']['passed'] for r in observations),
            release_acceptance=False)
    except Exception as error:result['error']=repr(error)
    finally:
        result.update(seconds=time.monotonic()-started,defaults_unchanged=defaults()==plan['defaults'])
        if not result['defaults_unchanged'] or result['seconds']>LIMITS['seconds']:result['status']='FAILED'
        save(out/'result.json',result)
    print(json.dumps(result));return 0 if result['status'].startswith('COMPLETED_') else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='mode',required=True)
    p=sub.add_parser('prepare');p.add_argument('--binary',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('run');p.add_argument('--plan',type=Path,required=True)
    args=parser.parse_args()
    if args.mode=='prepare':prepare(args)
    else:raise SystemExit(run(args.plan.resolve(strict=True)))
