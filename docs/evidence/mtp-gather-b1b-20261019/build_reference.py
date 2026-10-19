import subprocess, shlex, shutil, hashlib, json
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor'); out=Path(__file__).resolve().parent; build=out/'build'; ref=out/'reference'; ref.mkdir(exist_ok=True)
def run(cmd):
    print(shlex.join(cmd),flush=True); subprocess.run(cmd,cwd=build,check=True)
def flags(target,prefix):
    data=(build/f'CMakeFiles/{target}.dir/flags.make').read_text().splitlines()
    return sum((shlex.split(next(x.split('=',1)[1] for x in data if x.startswith(prefix+'_'+key+' ='))) for key in ['DEFINES','INCLUDES','FLAGS']),[])
source=subprocess.check_output(['git','show','a893dc8:src/mtp/mtp.cu'],cwd=root)
(ref/'mtp.cu').write_bytes(source)
run(['/usr/local/cuda-13.3/bin/nvcc','-forward-unknown-to-host-compiler','-ccbin=/usr/bin/g++-14',*flags('q4t_model','CUDA'),'-c',str(ref/'mtp.cu'),'-o',str(ref/'mtp.cu.o')])
run(['/usr/bin/g++-14',*flags('q4t_tests','CXX'),'-DQ4T_MTP_PARENT_REFERENCE','-c',str(root/'tests/mtp_spec_multi_test.cpp'),'-o',str(ref/'multi.o')])
shutil.copyfile(build/'libq4t_model.a',ref/'libq4t_model.a')
run(['ar','r',str(ref/'libq4t_model.a'),str(ref/'mtp.cu.o')])
cmd=shlex.split((build/'CMakeFiles/q4t_tests.dir/link.txt').read_text())
cmd=[str(ref/'libq4t_model.a') if x=='libq4t_model.a' else str(ref/'multi.o') if x=='CMakeFiles/q4t_tests.dir/tests/mtp_spec_multi_test.cpp.o' else str(ref/'q4t_tests') if x=='q4t_tests' else x for x in cmd]
run(cmd)
(ref/'identity.json').write_text(json.dumps({str(p.relative_to(out)):hashlib.sha256(p.read_bytes()).hexdigest() for p in ref.iterdir() if p.is_file()},indent=2)+'\n')
