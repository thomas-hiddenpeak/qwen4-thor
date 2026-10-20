"""Reproduce the accepted binary before adding research linkage."""
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT/'.q4t-work/vmm-compare-20261020'
BUILD = ROOT/'.q4t-work/offload-foundation-20261020/build'
SOURCE = Path(__file__).with_name('memory_trace.cpp')
APIS = ['cudaMalloc','cudaFree','cudaHostAlloc','cudaMallocHost','cudaFreeHost',
        'cudaMallocAsync','cudaFreeAsync','cudaStreamSynchronize',
        'cudaDeviceSynchronize','cudaEventRecord','cudaEventSynchronize']

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    BASE.mkdir(exist_ok=True)
    command=[s for s in shlex.split((BUILD/'CMakeFiles/q4t.dir/link.txt').read_text())
             if not s.startswith('-Wl,--dependency-file=')]
    command[command.index('-o')+1]=str(BASE/'unwrapped')
    subprocess.run(command,cwd=BUILD,check=True)
    expected='19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'
    assert sha(BASE/'unwrapped')==sha(ROOT/'build/q4t')==expected
    manifest=dict(default=expected,unwrapped=sha(BASE/'unwrapped'),
                  source=sha(SOURCE),vmm_header=sha(SOURCE.parent.parent/'vmm/block.h'),arms={})
    for name in ('runtime','vmm'):
        out=BASE/name
        out.mkdir(exist_ok=True)
        compile=['g++-14','-std=c++23','-O2','-Wall','-Wextra','-Werror',
                 '-I/usr/local/cuda-13.3/include','-c',str(SOURCE),'-o',str(out/'trace.o')]
        if name=='vmm':compile.append('-DQ4T_VMM_LINKED')
        subprocess.run(compile,check=True)
        link=command.copy()
        link[link.index('-o')+1]=str(out/'q4t')
        link += [str(out/'trace.o'),*[f'-Wl,--wrap={api}' for api in APIS],'-lcuda']
        subprocess.run(link,cwd=BUILD,check=True)
        shutil.copy2(BUILD/'CMakeCache.txt',out/'CMakeCache.txt')
        contract_source=SOURCE.with_name('contract.cpp')
        contract=['g++-14','-std=c++23','-O2','-Wall','-Wextra','-Werror',
            '-I/usr/local/cuda-13.3/include',str(contract_source),str(out/'trace.o'),
            '-L/usr/local/cuda-13.3/lib64/stubs','-L/usr/local/cuda-13.3/lib64',
            '-Wl,-rpath,/usr/local/cuda-13.3/lib64','-lcuda','-lcudart',
            '-o',str(out/'contract'),*[f'-Wl,--wrap={api}' for api in APIS]]
        subprocess.run(contract,check=True)
        manifest['contract_source']=sha(contract_source)
        manifest['arms'][name]=dict(binary=sha(out/'q4t'),object=sha(out/'trace.o'),
                                    compile=compile,link=link,contract=sha(out/'contract'),
                                    contract_compile=contract)
    (BASE/'build.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps({name:arm['binary'] for name,arm in manifest['arms'].items()}))

if __name__=='__main__':main()
