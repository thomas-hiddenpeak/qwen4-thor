"""Relink the exact accepted objects; refuse a mismatched baseline."""
import subprocess,shlex,hashlib,json,shutil
from pathlib import Path
out=Path('.q4t-work/warm-start-20261020').resolve()
source=Path('tools/verify/inference_metadata/warm_alloc_observer.cpp').resolve()
subprocess.run(['g++-14','-std=c++23','-Wall','-Wextra','-Werror','-O2','-I/usr/local/cuda-13.3/include','-c',str(source),'-o',str(out/'observer.o')],check=True)
cmd=shlex.split(Path('.q4t-work/offload-foundation-20261020/build/CMakeFiles/q4t.dir/link.txt').read_text())
cmd=[x for x in cmd if not x.startswith('-Wl,--dependency-file=')]
cmd[cmd.index('-o')+1]=str(out/'unwrapped')
subprocess.run(cmd,cwd='.q4t-work/offload-foundation-20261020/build',check=True)
sha=lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
assert sha(out/'unwrapped')==sha('build/q4t'), 'objects do not reproduce default'
cmd[cmd.index('-o')+1]=str(out/'q4t')
cmd += [str(out/'observer.o'),'-Wl,--wrap=cudaMalloc,--wrap=cudaHostAlloc,--wrap=cudaMallocHost']
subprocess.run(cmd,cwd='.q4t-work/offload-foundation-20261020/build',check=True)
shutil.copy2('.q4t-work/offload-foundation-20261020/build/CMakeCache.txt',out/'CMakeCache.txt')
(out/'build-identity.json').write_text(json.dumps(dict(default_sha256=sha('build/q4t'),unwrapped_sha256=sha(out/'unwrapped'),observer_sha256=sha(out/'q4t'),source_sha256=sha(source),link_command=cmd),indent=2)+'\n')
print((out/'build-identity.json').read_text()[:350])
