from pathlib import Path
import hashlib,json,fcntl,socket,shutil,os,sys
sys.dont_write_bytecode=True
ROOT=Path.cwd();sys.path.insert(0,str(ROOT))
from tools.serve_service import package as pkg
from tools.serve_recovery import bundle
base=ROOT/'.q4t-work/serve-tool-stream-20261108';business=base/'business-01';release=base/'release-01'
read=pkg.read;save=pkg.save;sha=pkg.sha;require=pkg.require
result=read(base/'delivery-01/result.json');outer=read(business/'lease/exit.json');plan=read(business/'plan.json')
bundle.verify(result['package'],result['manifest_sha256'])
for name in ('q4t','q4t.release.json','service.release.json'):
 require(sha(ROOT/'build'/name)==sha((release/'engine' if name!='service.release.json' else base/'delivery-01')/name),'install mismatch '+name)
for name,digest in plan['sources'].items():require(sha(base/'source'/name)==digest,'source snapshot mismatch')
with Path(outer['lock_path']).open('a+b') as f:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
for port in (18161,18162,18163,18164,18165):
 with socket.socket() as sock:
  sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind(('127.0.0.1',port))
require(sha(ROOT/'build/service-native-state/ledger.json')==result['ledger_sha256'],'ledger drift')
require(not any(Path('/proc',pid).exists() for pid in read(business/'cleanup.json')['owned']),'owned PID remains')
# Initial old capability-count fixture/import failures retained; repaired only affected checks.
cpu1=read(base/'cpu-01/result.json')
require(all(cpu1[k]['exit_code']==0 for k in ('shapes','js','schema','sources')),'CPU accepted rows changed')
require(all(read(base/name)['exit_code']==0 for name in ('cpu-02/host.result.json','cpu-02/python.result.json','cpu-03/result.json')),'CPU repair/driver failed')
save(base/'audit.json',dict(status='PASS_TARGETED_TOOL_STREAM_SOURCE_RELEASE_AUDIT',source_commit=plan['source_commit'],source_files=len(plan['sources']),protocol=read(release/'decision.json')['protocol_sha256'],binary_sha256=plan['binary_sha256'],ports_free=[18161,18162,18163,18164,18165],gpu_lock_acquirable=True,owned_pids_exited=True,ledger_unchanged=True,remaining_master_tasks_open=True,tool_stream_targeted_accepted=True,receipt_path=str(base/'delivery-01/result.json'),receipt_sha256=sha(base/'delivery-01/result.json'),cpu_dependency_reuse='31 C++,28 schema/native,9 Python,4 JS and3 driver passed; unchanged prediction/sampling/native/recovery evidence reused. First driver output path, old capability fixture and Python import failures preserved.'))
archive=ROOT/'docs/evidence/serve-tool-stream-20261108';archive.mkdir(parents=True)
files=[]
for pattern in ('build-*','cpu-*','native-build-*','business-*'):
 for directory in base.glob(pattern):
  if directory.is_dir():files.extend(p for p in directory.rglob('*') if p.is_file())
for p in base.iterdir():
 if p.is_file() and p.suffix in ('.py','.json','.log'):files.append(p)
for name in ('decision.json','receipt.json','native-build.json','unchanged-control.json','engine/runtime-source.json','engine/q4t.release.json'):
 files.append(release/name)
files.extend(p for p in (release/'engine/declarations').rglob('*') if p.is_file())
for p in (base/'delivery-01').rglob('*'):
 if p.is_file() and p.name!='q4t':files.append(p)
rows={}
for p in sorted(set(files)):
 require('__pycache__' not in p.parts and p.stat().st_size<16*1024*1024,'unexpected artifact '+str(p))
 require(p.read_bytes()[:4]!=b'\x7fELF','binary in archive')
 rel=p.relative_to(base);target=archive/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,target)
 require(sha(p)==sha(target),'archive mismatch')
 rows[str(rel)]=dict(sha256=sha(target),bytes=target.stat().st_size,source=str(p))
save(archive/'index.json',dict(version=1,source_commit=plan['source_commit'],files=rows,excluded=['ELF/model assets','release bundle private gateway/client policies','source snapshot (Git commit and plan source digests retained)']))
print(json.dumps(dict(raw_files=len(rows),bytes=sum(r['bytes'] for r in rows.values()),audit=read(base/'audit.json'))))
