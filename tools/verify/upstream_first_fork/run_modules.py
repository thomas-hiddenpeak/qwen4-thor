"""Capture loaded projection modules once with a bounded CUPTI observer."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from run_trace import PRIOR, ROOT, sha

OUTPUT = ROOT/'.q4t-work/upstream-cublas-modules-20261020/run-v1'
OBSERVER = OUTPUT.parent/'observer.so'


def main():
    OUTPUT.mkdir(exist_ok=False)
    state = dict(status='PREPARING', started=time.time())
    try:
        files = json.loads((PRIOR/'files.json').read_text())
        names = ['executable'] + ['fixture/'+p.name for p in (PRIOR/'fixture').iterdir()]
        for name in names:
            if sha(PRIOR/name) != files[name]:
                raise ValueError('frozen input changed')
        shutil.copyfile(OBSERVER, OUTPUT/'observer.so')
        shutil.copyfile(Path(__file__), OUTPUT/'driver.py')
        shutil.copyfile(Path(__file__).with_name('module_observer.cpp'), OUTPUT/'observer.cpp')
        (OUTPUT/'modules').mkdir()
        plan = dict(model_loads=0, gemv_calls=4, bf16_batch_calls=1,
                    float_output_calls=1, runs=1, retries=0, max_modules=4,
                    max_module_bytes=256*1024**2, max_total_bytes=512*1024**2,
                    observer_sha256=sha(OUTPUT/'observer.so'),
                    prior_files={name:files[name] for name in names})
        command = [str(PRIOR/'executable'), str(PRIOR/'fixture'), str(OUTPUT/'projection')]
        plan['command'] = command
        (OUTPUT/'plan.json').write_text(json.dumps(plan, indent=2)+'\n')
        env = dict(os.environ, LD_PRELOAD=str(OUTPUT/'observer.so'),
                   UPSTREAM_CUBIN_OUTPUT=str(OUTPUT/'modules'))
        state['status'] = 'RUNNING'
        with (OUTPUT/'capture.log').open('w') as log:
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode:
            raise ValueError('capture exit '+str(result.returncode))
        modules = list((OUTPUT/'modules').glob('*.cubin'))
        if not 1 <= len(modules) <= 4:
            raise ValueError('missing or excess captured modules')
        for name in ['gemv.bf16','gemv.f32','t4.bf16','float_output.f32','result.json']:
            if (OUTPUT/'projection'/name).read_bytes() != (PRIOR/'projection'/name).read_bytes():
                raise ValueError('capture perturbed output '+name)
        for name in names:
            if sha(PRIOR/name) != files[name]:
                raise ValueError('frozen input changed')
        state.update(status='PASS', modules=len(modules))
    except BaseException as error:
        state.update(status='INTERRUPTED' if isinstance(error, KeyboardInterrupt) else 'FAIL', error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        (OUTPUT/'run.json').write_text(json.dumps(state, indent=2)+'\n')
        manifest = {str(p.relative_to(OUTPUT)):sha(p) for p in OUTPUT.rglob('*') if p.is_file()}
        (OUTPUT/'files.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print('PASS loaded module capture; original outputs unchanged', flush=True)


if __name__ == '__main__':
    main()
