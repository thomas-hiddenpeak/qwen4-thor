import hashlib,json,os,subprocess,tarfile,time
from pathlib import Path
R=Path(__file__).resolve().parent
W=R/'source';S=R/'candidate-source';B=R/'candidate-build'
assert not S.exists() and not B.exists()
assert not subprocess.check_output(['git','status','--porcelain'],cwd=W).strip(), 'commit intended source first'
commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=W,text=True).strip()
a=R/'candidate-source.tar'
with a.open('xb') as f:subprocess.run(['git','archive',commit],cwd=W,stdout=f,check=True)
S.mkdir()
with tarfile.open(a) as tar:tar.extractall(S,filter='data')
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(name,d):
 with (R/name).open('x') as f:json.dump(d,f,indent=2);f.write('\n')
save('candidate-source-identity.json',dict(commit=commit,source=str(S),clean_export=True,excluded_original_dirty_paths=7,created_t=time.time(),archive_sha256=sha(a)))
env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
commands=[('configure',['cmake','-S',str(S),'-B',str(B),'-DCMAKE_BUILD_TYPE=Release','-DCMAKE_CXX_COMPILER=/usr/bin/g++-14','-DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-14','-DQ4T_CUDA_ARCHITECTURES=110a','-DCMAKE_EXPORT_COMPILE_COMMANDS=ON']),('build',['cmake','--build',str(B),'--target','q4t','--parallel','8'])]
records=[]
for name,cmd in commands:
 start=time.time()
 with (R/(name+'.log')).open('x') as f:rc=subprocess.run(cmd,cwd=W,env=env,stdout=f,stderr=subprocess.STDOUT).returncode
 d=dict(name=name,command=cmd,started_t=start,ended_t=time.time(),returncode=rc);records.append(d);save(name+'-exit.json',d);print(json.dumps(d),flush=True)
 if rc:raise SystemExit(rc)
warnings=sum((R/(n+'.log')).read_text().lower().count('warning:') for n in ('configure','build'))
d=dict(source_commit=commit,source=str(S),binary=str(B/'q4t'),binary_sha256=sha(B/'q4t'),cmake_cache_sha256=sha(B/'CMakeCache.txt'),compile_commands_sha256=sha(B/'compile_commands.json'),warnings=warnings,build_rc=0,records=records,runtime_tests_before_http=False,ended_t=time.time())
save('build-identity.json',d);print(json.dumps(d),flush=True)
raise SystemExit(0 if warnings==0 else 2)
