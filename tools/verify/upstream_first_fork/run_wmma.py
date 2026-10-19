"""One independent WMMA run: real fixture, zero and sparse basis controls."""
from fractions import Fraction
import json
from pathlib import Path
import shutil
import struct
import subprocess
import time
from reference import nearest_bf16
from run_trace import PRIOR, ROOT, sha

OUTPUT = ROOT/'.q4t-work/upstream-wmma-20261020/run-v1'


def main():
    OUTPUT.mkdir(exist_ok=False)
    state = dict(status='PREPARING', started=time.time())
    try:
        files = json.loads((PRIOR/'files.json').read_text())
        names = ['fixture/x', 'fixture/weight', 'projection/float_output.f32', 'projection/t4.bf16']
        for name in names:
            if sha(PRIOR/name) != files[name]:
                raise ValueError('frozen input changed')
        shutil.copyfile(OUTPUT.parent/'executable', OUTPUT/'executable')
        (OUTPUT/'executable').chmod(0o755)
        for name in ['run_wmma.py', 'wmma_reference.cu', 'reference.py', 'run_trace.py']:
            shutil.copyfile(Path(__file__).with_name(name), OUTPUT/name)
        plan = dict(model_loads=0, launches=3, real_fixture_calls=1,
                    controls=['zero', 'sparse basis at K=0,15,16,2559'], retries=0,
                    binary_sha256=sha(OUTPUT/'executable'),
                    prior_files={name:files[name] for name in names})
        (OUTPUT/'plan.json').write_text(json.dumps(plan, indent=2)+'\n')
        state['status'] = 'RUNNING'
        with (OUTPUT/'gpu.log').open('w') as log:
            subprocess.run([str(OUTPUT/'executable'), str(PRIOR/'fixture'), str(OUTPUT/'output')],
                           cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
        raw = (OUTPUT/'output/0.f32').read_bytes()[:192*4]
        expected = (PRIOR/'projection/float_output.f32').read_bytes()
        floats = struct.unpack('<192f', raw)
        bf16 = struct.pack('<192H', *[nearest_bf16(Fraction(f)) for f in floats])
        checks = dict(float_all_192_bitexact=raw == expected,
                      bf16_all_192_bitexact=bf16 == (PRIOR/'projection/t4.bf16').read_bytes())
        (OUTPUT/'comparison.json').write_text(json.dumps(dict(checks=checks,
            head43=[floats[row*48+43] for row in range(4)]), indent=2)+'\n')
        if not all(checks.values()):
            raise ValueError('independent WMMA differs; preserve for analysis')
        for name in names:
            if sha(PRIOR/name) != files[name]:
                raise ValueError('frozen input changed')
        state['status'] = 'PASS'
    except BaseException as error:
        state.update(status='INTERRUPTED' if isinstance(error, KeyboardInterrupt) else 'FAIL', error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        (OUTPUT/'run.json').write_text(json.dumps(state, indent=2)+'\n')
        manifest = {str(p.relative_to(OUTPUT)):sha(p) for p in OUTPUT.rglob('*') if p.is_file()}
        (OUTPUT/'files.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print('PASS independent WMMA matches all 192 float and BF16 results', flush=True)


if __name__ == '__main__':
    main()
