"""One frozen local projection run; execute the whole driver under GPU lease."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time

from fp32_reference import gemv_dot
from reference import nearest_bf16

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / '.q4t-work/upstream-projection-20261020'
PRIOR = ROOT / '.q4t-work/upstream-first-fork-20261019/gpu-v1'
BINARY = ROOT / '.q4t-work/moe-replay-a1-20261019/build/q4t_upstream_projection'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    output = BASE / 'gpu-v1'
    output.mkdir(exist_ok=False)
    state = dict(status='PREPARING', started=time.time())
    try:
        if any(k.startswith('Q4T_') for k in os.environ):
            raise ValueError('unset Q4T overrides')
        manifest = json.loads((PRIOR / 'files.json').read_text())
        fixture = output / 'fixture'
        fixture.mkdir()
        inputs = {'weight': 'layer0.gdn.a.weight', 'x': 'p72-t4-l0.gdn.a.x',
                  't4': 'p72-t4-l0.gdn.a.y'}
        provenance = {}
        def read(name):
            key = 'capture/upstream/' + name
            path = PRIOR / key
            if sha(path) != manifest[key]:
                raise ValueError('prior capture changed: ' + name)
            provenance[key] = manifest[key]
            return path.read_bytes()
        for target, name in inputs.items():
            (fixture / target).write_bytes(read(name))
        t1 = b''.join(read(f'p{72+i}-t1-l0.gdn.a.y') for i in range(4))
        (fixture / 't1').write_bytes(t1)
        x1 = b''.join(read(f'p{72+i}-t1-l0.gdn.a.x') for i in range(4))
        if x1 != (fixture / 'x').read_bytes():
            raise ValueError('unequal actual inputs')
        executable = output / 'executable'
        shutil.copy2(BINARY, executable)
        sources = subprocess.check_output(['git', 'ls-files', 'src', 'include',
            'CMakeLists.txt'], cwd=ROOT, text=True).splitlines()
        sources += [str(p.relative_to(ROOT)) for p in Path(__file__).parent.glob('*') if p.is_file()]
        sources += ['tools/verify/moe_replay_real/capture.cpp',
                    'tools/verify/moe_replay_real/capture_context.h']
        source_hashes = {}
        for name in sorted(set(sources)):
            dest = output / 'source' / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, dest)
            source_hashes[name] = sha(dest)
        fixture_hashes = {p.name: sha(p) for p in fixture.iterdir()}
        plan = dict(model_loads=0, full_model_forwards=0, gemv_fp32_calls=4,
                    bf16_batch_calls=1, float_output_calls=1, retries=0,
                    gpu_binary_sha256=sha(executable), input_provenance=provenance,
                    fixture=fixture_hashes, source=source_hashes,
                    source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                    limitation='Float-output algorithm may differ; never substitute its FP32 values for BF16 kernel internals.')
        (output / 'plan.json').write_text(json.dumps(plan, indent=2)+'\n')
        command = [str(executable), str(fixture), str(output / 'projection')]
        state.update(status='RUNNING', command=command)
        (output / 'run.json').write_text(json.dumps(state, indent=2)+'\n')
        with (output / 'gpu.log').open('w') as log:
            result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode:
            raise ValueError(f'projection exit {result.returncode}')
        x = struct.unpack('<10240H', (fixture/'x').read_bytes())
        w = struct.unpack('<122880H', (fixture/'weight').read_bytes())
        fp32_raw = (output/'projection/gemv.f32').read_bytes()
        bf16 = struct.unpack('<192H', (output/'projection/gemv.bf16').read_bytes())
        checks = []
        for row in range(4):
            exact = gemv_dot(x[row*2560:(row+1)*2560], w[43*2560:44*2560])
            expected = struct.pack('<f', float(exact))
            actual = fp32_raw[(row*48+43)*4:(row*48+44)*4]
            if actual != expected or nearest_bf16(exact) != bf16[row*48+43]:
                raise ValueError('independent FP32 reduction mismatch')
            checks.append(dict(row=row, head=43, fp32=float(exact),
                               fp32_hex=actual.hex(), bf16=bf16[row*48+43]))
        for name, digest in source_hashes.items():
            if sha(ROOT/name) != digest or sha(output/'source'/name) != digest:
                raise ValueError('source changed')
        for name, digest in fixture_hashes.items():
            if sha(fixture/name) != digest:
                raise ValueError('fixture changed')
        if sha(executable) != plan['gpu_binary_sha256']:
            raise ValueError('executable changed')
        state.update(status='PASS', fp32_reference=checks)
    except BaseException as error:
        state.update(status='INTERRUPTED' if isinstance(error, KeyboardInterrupt) else 'FAIL', error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        (output/'run.json').write_text(json.dumps(state, indent=2)+'\n')
        files = {str(p.relative_to(output)): sha(p) for p in output.rglob('*') if p.is_file()}
        (output/'files.json').write_text(json.dumps(files, indent=2)+'\n')
    print('PASS local projection and exact FP32 GEMV reference', flush=True)


if __name__ == '__main__':
    main()
