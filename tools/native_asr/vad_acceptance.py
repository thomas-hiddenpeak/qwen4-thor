"""Frozen native/official VAD CPU comparison; no CUDA calls or GPU tests."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import time
import traceback


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(value, message):
    if not value:
        raise RuntimeError(message)


def values(path):
    raw = path.read_bytes()
    require(raw and len(raw) % 4 == 0, 'float result shape')
    return struct.unpack('<'+'f'*(len(raw)//4), raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    args.output.mkdir(exist_ok=False)
    started = time.monotonic()
    result = dict(status='FAILED', gpu_actions=0, reference_device='cpu')
    try:
        for name, expected in plan['identities'].items():
            require(digest(name) == expected, 'source/asset/binary drift: '+name)
        for name, expected in plan['protected'].items():
            require(digest(name) == expected, 'published default drift')
        jobs = [
            ('unit', [plan['unit_binary'], '--required-list', plan['required_list']]),
            ('native', [plan['native_binary'], plan['native_model'],
                        str(args.plan), str(args.output/'native')]),
            ('reference', [plan['reference_python'], '-B', plan['reference_source'],
                '--plan', str(args.plan), '--output', str(args.output/'reference')])]
        if plan.get('reuse_native'):
            prior = Path(plan['reuse_native']['directory'])
            old_plan = json.loads((prior.parent/'compute-plan.json').read_text())
            old_result = json.loads((prior/'result.json').read_text())
            require(old_result['status'] == 'FAILED' and
                    old_result['default_unchanged'] and
                    'reference failed' in old_result['error'],
                    'unexpected original failure')
            require('8 tests, 8 passed, 0 failed, 0 skipped' in
                    (prior/'unit.log').read_text(), 'original CPU contracts failed')
            for name, expected in plan['reuse_native']['files'].items():
                require(digest(name) == expected, 'retained evidence drift')
            for name, expected in old_plan['identities'].items():
                if Path(name).name not in ('vad_reference.py', 'vad_acceptance.py'):
                    require(digest(name) == expected, 'unchanged native identity drift')
            require(old_plan['absolute_error'] == plan['absolute_error'] and
                    old_plan['cases'] == plan['cases'], 'changed original contract')
            shutil.copytree(prior/'native', args.output/'native')
            shutil.copyfile(prior/'unit.log', args.output/'unit.log')
            shutil.copyfile(prior/'native.log', args.output/'native.log')
            jobs = jobs[-1:]
            result.update(reused_unit_tests=8, reused_native_cases=4,
                          original_run_status='FAILED_preserved',
                          new_native_model_runs=0, new_reference_model_runs=1)
        env = dict(os.environ, CUDA_VISIBLE_DEVICES='', PYTHONDONTWRITEBYTECODE='1',
                   OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
        for label, command in jobs:
            with (args.output/(label+'.log')).open('xb') as log:
                run = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                    env=env, timeout=max(.01, plan['seconds']-(time.monotonic()-started)))
            require(run.returncode == 0, label+' failed')
        comparisons = []
        for index, case in enumerate(plan['cases']):
            record = dict(case=case['name'], samples=case['samples'], tensors={})
            for tensor in ('probabilities', 'hidden', 'cell'):
                name = f'case-{index}-{tensor}.f32'
                a, b = [values(args.output/k/name) for k in ('native', 'reference')]
                size = math.ceil(case['samples']/512) if tensor == 'probabilities' else 128
                require(len(a) == len(b) == size, 'native/reference result size')
                require(all(math.isfinite(v) for v in a+b), 'nonfinite native/reference')
                error = max(abs(x-y) for x, y in zip(a, b))
                record['tensors'][tensor] = dict(values=len(a), max_absolute_error=error)
                comparisons.append(dict(case=case['name'], tensor=tensor,
                    maximum_error=error, bound=plan['absolute_error'][tensor]))
                (args.output/'comparisons.json').write_text(json.dumps(comparisons, indent=2)+'\n')
                require(error <= plan['absolute_error'][tensor], 'CPU numerical contract: '+name)
                if tensor == 'probabilities':
                    require(all(0 <= x <= 1 for x in a), 'probability range')
                    require(all((x >= .5) == (y >= .5) for x, y in zip(a, b)),
                            'speech frame decisions differ')
                    record['speech_frames'] = sum(x >= .5 for x in a)
                    record['speech_probability_min'] = min(a)
                    record['speech_probability_max'] = max(a)
                    if case['name'] in ('zh', 'en'):
                        require(max(a) > .5, 'real speech never detected')
                    if case['name'] == 'silence':
                        require(max(a) <= .1, 'zero PCM detected as speech')
            (args.output/(case['name']+'-result.json')).write_text(json.dumps(record, indent=2)+'\n')
        result.update(status='PASS_NATIVE_VAD_CPU_COMPONENT', unit_tests=8,
                      actual_cases=4, comparisons=comparisons)
    except BaseException:
        result['error'] = traceback.format_exc()
        raise
    finally:
        result['seconds'] = time.monotonic()-started
        result['default_unchanged'] = all(digest(p) == h for p, h in plan['protected'].items())
        (args.output/'result.json').write_text(json.dumps(result, indent=2)+'\n')


if __name__ == '__main__':
    main()
