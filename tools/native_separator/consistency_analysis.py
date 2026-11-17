"""CPU-only one-candidate analysis of two retained mixtures; no model execution."""
import argparse
import json
from pathlib import Path
import sys
import time
import wave

import numpy as np

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.native_separator.acceptance import separation_quality
from tools.native_separator.http_acceptance import WORK
from tools.native_speaker.acceptance import defaults,digest


def main(output):
    output = output.resolve();output.mkdir(exist_ok=False)
    prior = json.loads((WORK/'component-02/plan.json').read_bytes())
    http = json.loads((WORK/'http-04/plan.json').read_bytes())
    files = [Path(__file__).resolve(), ROOT/'tools/native_separator/acceptance.py',
             WORK/'component-02/plan.json',WORK/'component-02/known-sources.f32',
             Path(prior['output'])/'run/native/case-2/sources.f32',
             WORK/'track-diagnostic-01/native/case-1/sources.f32',
             WORK/'track-diagnostic-01/case-1.f32']
    case = prior['cases'][2]
    assert digest(case['known_sources']) == case['known_sources_sha256']
    assert digest(case['pcm']) == case['pcm_sha256']
    known = np.fromfile(case['known_sources'],'<f4').reshape(2,-1).astype(np.float64)
    pure = []
    for name in ['spk1_snt1.wav','spk2_snt1.wav']:
        record = http['clips'][name];path = Path(record['path'])
        assert digest(path) == record['sha256'];files.append(path)
        with wave.open(str(path),'rb') as clip:
            assert (clip.getnchannels(),clip.getsampwidth(),clip.getframerate()) == (1,2,16000)
            pcm = np.frombuffer(clip.readframes(clip.getnframes()),'<i2').astype(np.float64)
        raw = np.zeros(64000)
        raw[3200:3200+len(pcm)] = np.rint(pcm*.5)/32768.
        axis = np.arange(-32,33,dtype=np.float64)
        kernel = .45*np.sinc(.45*axis)*np.hanning(65);kernel /= kernel.sum()
        pure.append(np.convolve(raw,kernel,mode='full')[32:32+len(raw)][::2].astype('<f4'))
    targets = np.asarray(pure,dtype=np.float64)
    cases = [('original_component',known,Path(case['pcm']),files[4]),
             ('http_integer_mix',targets,files[6],files[5])]
    files += [Path(case['pcm'])]
    protected = defaults()
    plan = dict(candidate='two-source signed least-squares gain, then equal residual projection',
        reference='https://arxiv.org/abs/1811.08521',
        distinction='paper equation 7 gives projection; gain fit is local adaptation for arbitrary source scale',
        limits=dict(cases=2,candidates=1,seconds=60,gpu=0,http=0),
        acceptance='each source SI-SDR must not decrease; each improvement over mixture remains >=5dB',
        clean_sources_used_only_for_scoring=True,
        identities={str(p):digest(p) for p in files},defaults=protected,numpy=np.__version__)
    (output/'plan.json').write_text(json.dumps(plan,indent=2)+'\n')
    results=[];started=time.monotonic()
    for label,truth,mix_path,estimate_path in cases:
        mixture=np.fromfile(mix_path,'<f4').astype(np.float64)
        estimates=np.fromfile(estimate_path,'<f4').reshape(2,-1).astype(np.float64)
        assert truth.shape == estimates.shape == (2,32000)
        assert np.max(np.abs(truth.sum(axis=0)-mixture)) < 1e-7
        gram=estimates@estimates.T
        determinant_ratio=float(np.linalg.det(gram)/(gram[0,0]*gram[1,1]))
        assert determinant_ratio > 1e-8
        gains=np.linalg.solve(gram,estimates@mixture)
        scaled=estimates*gains[:,None]
        residual=mixture-scaled.sum(axis=0)
        projected=(scaled+residual[None,:]/2).astype('<f4').astype(np.float64)
        before=separation_quality(truth,estimates)
        after=separation_quality(truth,projected)
        retained=all(b >= a for a,b in zip(before['separated_si_sdr_db'],after['separated_si_sdr_db']))
        row=dict(case=label,gains=gains.tolist(),determinant_ratio=determinant_ratio,
            relative_residual_rms=float(np.linalg.norm(residual)/np.linalg.norm(mixture)),
            projected_sum_max_error=float(np.max(abs(projected.sum(axis=0)-mixture))),
            before=before,after=after,both_sources_not_worse=retained)
        results.append(row)
    result=dict(status='SIGNAL_CANDIDATE' if all(r['both_sources_not_worse'] and r['after']['passed'] for r in results)
                else 'REJECTED_SIGNAL_CANDIDATE',observations=results,seconds=time.monotonic()-started,
                defaults_unchanged=defaults()==protected,release_acceptance=False)
    assert result['seconds']<60 and result['defaults_unchanged']
    (output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    main(parser.parse_args().output)
