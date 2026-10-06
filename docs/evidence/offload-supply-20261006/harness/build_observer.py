"""Clean committed export and owned local Thor build; no tests/model."""
from pathlib import Path
import re
import subprocess
import tarfile
import time
from observer_common import R, O, run, save, sha

assert not subprocess.check_output(['git','status','--porcelain'],cwd=O)
commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=O,text=True).strip()
source=R/'observer-export';build=R/'observer-build';archive=R/'observer-source.tar'
assert not source.exists() and not build.exists() and not archive.exists()
subprocess.run(['git','archive','--format=tar','-o',str(archive),commit],cwd=O,check=True)
source.mkdir()
with tarfile.open(archive) as stream:stream.extractall(source,filter='data')
sources={str(p):sha(p) for p in sorted(source.rglob('*')) if p.is_file()}
save(R/'observer-source-identity.json',{'commit':commit,'source':str(source),'clean_export':True,
    'archive_sha256':sha(archive),'source_sha256':sources,'created_t':time.time()})
commands=[('observer-configure-01',['cmake','-S',str(source),'-B',str(build),'-DCMAKE_BUILD_TYPE=Release',
    '-DCMAKE_CXX_COMPILER=/usr/bin/g++-14','-DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-14',
    '-DQ4T_CUDA_ARCHITECTURES=110a','-DCMAKE_EXPORT_COMPILE_COMMANDS=ON']),
    ('observer-build-01',['cmake','--build',str(build),'--target','q4t','--parallel','8'])]
records=[];failure=None
try:
    for label,command in commands:
        result=run(command,R,label,cwd=O,timeout=600)
        records.append(result)
        assert result['returncode']==0 and result['failure'] is None and result['cleanup_complete'],label
    warnings=sum(len(re.findall(r'warning\s*:|warning\s*#|cmake\s+warning',(R/(label+'.log')).read_text(),re.I)) for label,_ in commands)
    assert warnings==0
    for path,digest in sources.items():assert sha(path)==digest,path
    save(R/'observer-build-identity.json',{'source_commit':commit,'source':str(source),'binary':str(build/'q4t'),
        'binary_sha256':sha(build/'q4t'),'cmake_cache_sha256':sha(build/'CMakeCache.txt'),
        'compile_commands_sha256':sha(build/'compile_commands.json'),'source_sha256':sources,
        'warnings':warnings,'build_rc':0,'records':records,'tests_or_model_executed':False,'ended_t':time.time()})
except BaseException as error:
    failure=type(error).__name__+': '+str(error)
    raise
finally:
    save(R/'observer-build-attempt.json',{'source_commit':commit,'records':records,'failure':failure,'ended_t':time.time()})
print('observer local build zero warnings; no tests/models executed',flush=True)
