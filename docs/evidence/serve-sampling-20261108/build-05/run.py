from pathlib import Path
import subprocess, json, time
root=Path.cwd(); out=root / '.q4t-work/serve-sampling-20261108/build-05'
build=root / '.q4t-work/serve-sampling-20261108/engine-build'
deps=root / '.q4t-work/serve-native-access-20261108/deps/extracted/usr/include'
steps=[['cmake','-S','.', '-B', str(build), '-DCMAKE_BUILD_TYPE=Release',
 '-DCMAKE_CXX_COMPILER=/usr/bin/g++-14','-DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-14',
 '-DQ4T_CUDA_ARCHITECTURES=110a','-DQ4T_ENABLE_XGRAMMAR=ON',
 '-DFETCHCONTENT_SOURCE_DIR_Q4T_XGRAMMAR='+str(root / '.q4t-work/structured-control-20261102/xgrammar'),
 '-DQ4T_OPENSSL_INCLUDE_DIR='+str(deps), '-DQ4T_OPENSSL_ARCH_INCLUDE_DIR='+str(deps / 'aarch64-linux-gnu')],
 ['cmake','--build',str(build),'--parallel','4','--target','q4t','q4t_preflight_probe','q4t_host_tests','q4t_contract_probe']]
rows=[]
for i,cmd in enumerate(steps):
 start=time.monotonic()
 with (out/f'{i}.log').open('x') as f: run=subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
 rows.append(dict(argv=cmd, exit_code=run.returncode, seconds=time.monotonic()-start))
 (out/'result.json').write_text(json.dumps(rows,indent=2)+'\n')
 print(i,run.returncode,flush=True)
 if run.returncode: raise SystemExit(run.returncode)
