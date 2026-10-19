"""One Nsight trace of the already-frozen local projection executable."""
import hashlib
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
PRIOR = ROOT/'.q4t-work/upstream-projection-20261020/gpu-v1'
OUTPUT = ROOT/'.q4t-work/upstream-cublas-trace-20261020/run-v1'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    OUTPUT.mkdir(exist_ok=False)
    state=dict(status='PREPARING',started=time.time())
    try:
        files=json.loads((PRIOR/'files.json').read_text())
        names=['executable']+['fixture/'+p.name for p in (PRIOR/'fixture').iterdir()]
        for name in names:
            if sha(PRIOR/name)!=files[name]:raise ValueError('frozen input changed')
        executable=PRIOR/'executable'
        nsys=Path('/usr/local/cuda/bin/nsys').resolve()
        plan=dict(model_loads=0,gemv_calls=4,bf16_batch_calls=1,float_output_calls=1,
                  trace_runs=1,retries=0,binary_sha256=sha(executable),
                  nsys=str(nsys),nsys_sha256=sha(nsys),
                  version=subprocess.check_output([str(nsys),'--version'],text=True).strip(),
                  prior_files={name:files[name] for name in names})
        command=[str(nsys),'profile','--trace=cuda','--sample=none','--cpuctxsw=none',
                 '--cuda-memory-usage=false','--output='+str(OUTPUT/'trace'),
                 str(executable),str(PRIOR/'fixture'),str(OUTPUT/'projection')]
        plan['command']=command
        (OUTPUT/'plan.json').write_text(json.dumps(plan,indent=2)+'\n')
        state['status']='RUNNING'
        with (OUTPUT/'profile.log').open('w') as log:
            result=subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        if result.returncode:raise ValueError('trace exit '+str(result.returncode))
        for name in ['gemv.bf16','gemv.f32','t4.bf16','float_output.f32','result.json']:
            if (OUTPUT/'projection'/name).read_bytes()!=(PRIOR/'projection'/name).read_bytes():
                raise ValueError('trace perturbed output '+name)
        with (OUTPUT/'export.log').open('w') as log:
            subprocess.run([str(nsys),'export','--type=sqlite','--output='+str(OUTPUT/'trace.sqlite'),
                            str(OUTPUT/'trace.nsys-rep')],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True)
        for name in names:
            if sha(PRIOR/name)!=files[name]:raise ValueError('frozen input changed')
        state['status']='PASS'
    except BaseException as error:
        state.update(status='INTERRUPTED' if isinstance(error,KeyboardInterrupt) else 'FAIL',error=repr(error))
        raise
    finally:
        state['finished']=time.time()
        (OUTPUT/'run.json').write_text(json.dumps(state,indent=2)+'\n')
        manifest={str(p.relative_to(OUTPUT)):sha(p) for p in OUTPUT.rglob('*') if p.is_file()}
        (OUTPUT/'files.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print('PASS trace, original outputs unchanged',flush=True)


if __name__=='__main__':main()
