"""Lossless sequential archive of six ended CPU fixture engines."""
from pathlib import Path
import hashlib,json,os,shutil,zipfile
W=Path(__file__).resolve().parent;R=W.parents[1];O=W/'cpu-binary-archive-14'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,v):p.write_text(json.dumps(v,indent=2)+'\n')
pairs=[['audio-export-cpu-01/q4t', '../serve-tts-icl-20261119/input-usage-cpu-01/q4t-before'], ['profile-contract-cpu-01/q4t-before', '../serve-tts-icl-20261119/recovery-contract-cpu-01/q4t'], ['history-context-cpu-01/q4t-before', '../serve-tts-icl-20261119/input-usage-cpu-01/q4t', '../serve-tts-icl-20261119/service-business-01/q4t'], ['history-retrieve-cpu-01/q4t', '../serve-tts-icl-20261119/recovery-contract-cpu-01/q4t-before'], ['../serve-semantic-vad-20261119/integration-cpu-02/failed-host-tests'], ['../serve-semantic-vad-20261119/integration-cpu-03/failed-host-tests']]
assert not O.exists();O.mkdir(mode=0o700);rows=[]
for paths in pairs:
 files=[(W/n).resolve() for n in paths];states=[p.stat() for p in files]
 assert all(s.st_ino==states[0].st_ino and s.st_nlink==len(files) for s in states)
 h=sha(files[0]);assert all(sha(p)==h for p in files)
 for p,s in zip(files,states):rows.append(dict(path=str(p),member=h,sha256=h,bytes=s.st_size,mode=s.st_mode&0o777,mtime_ns=s.st_mtime_ns,inode=s.st_ino,links=s.st_nlink))
protected={str(R/n):sha(R/n) for n in ['build/q4t','build/q4t.release.json','build/service/manifest.json','build/service.release.json','build/service-native-state/ledger.json','.q4t-work/serve-custom-tools-20261108/engine-build/q4t','.q4t-work/serve-file-search-20261112/host/q4t_host_tests']}
inodes={r['inode'] for r in rows}
for p in Path('/proc').iterdir():
 if not p.name.isdigit():continue
 try:s=(p/'exe').stat()
 except OSError:continue
 assert s.st_ino not in inodes,'fixture is still executing'
save(O/'plan.json',dict(scope='Six ended CPU fixture programs, all hardlinks fully inventoried; exact lossless byte/SHA archive. Current/default/recovery programs, models, source and logs kept.',files=rows,protected=protected,minimum_free_bytes=24*1024**2,maximum_member_archive_bytes=20*1024**2,physical_payloads=6))
archive=O/'payloads.zip';done=[]
class BoundedWriter:
 def __init__(self,path):
  self.file=path.open('r+b' if path.exists() else 'w+b')
  self.limit=path.stat().st_size+20*1024**2
 def __getattr__(self,name):return getattr(self.file,name)
 def write(self,data):
  if self.file.tell()+len(data)>self.limit or shutil.disk_usage(W).free-len(data)<24*1024**2:raise OSError('bounded archive storage guard')
  return self.file.write(data)
 def __enter__(self):return self
 def __exit__(self,*_):self.file.close()

try:
 for h in dict.fromkeys(r['member'] for r in rows):
  group=[r for r in rows if r['member']==h];assert shutil.disk_usage(W).free>=40*1024**2
  with BoundedWriter(archive) as stream:
   with zipfile.ZipFile(stream,'a',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    assert h not in z.namelist();z.write(group[0]['path'],h)
  with zipfile.ZipFile(archive) as z:
   assert z.getinfo(h).compress_size<=20*1024**2;data=z.read(h);assert hashlib.sha256(data).hexdigest()==h
   for r in group:assert Path(r['path']).read_bytes()==data
  for r in group:
   p=Path(r['path']);s=p.stat();assert s.st_ino==r['inode'] and sha(p)==h;p.unlink()
  done.append(h);print('archived',len(done),'free',shutil.disk_usage(W).free,flush=True)
 assert all(sha(Path(p))==h for p,h in protected.items())
finally:
 save(O/'result.json',dict(status='LOSSLESS_ARCHIVE_COMPLETE' if len(done)==len({r['member'] for r in rows}) else 'PARTIAL_ARCHIVE_PRESERVED',archived_members=done,archive_sha256=sha(archive) if archive.exists() else None,archive_bytes=archive.stat().st_size if archive.exists() else 0,freed_payload_bytes=sum(next(r['bytes'] for r in rows if r['inode']==ino) for ino in {r['inode'] for r in rows if r['member'] in done}),default_unchanged=all(sha(Path(p))==h for p,h in protected.items()),free=shutil.disk_usage(W).free,gpu_actions=0))
(O/'restore.py').write_text('''from pathlib import Path
import json,hashlib,zipfile,os,sys,shutil
w=Path(__file__).resolve().parent;p=json.loads((w/'plan.json').read_text());r=json.loads((w/'result.json').read_text());a=w/'payloads.zip'
assert hashlib.sha256(a.read_bytes()).hexdigest()==r['archive_sha256']
selected=set(sys.argv[1:]);assert selected and selected<={x['path'] for x in p['files']}
with zipfile.ZipFile(a) as z:
 for row in p['files']:
  if row['path'] not in selected:continue
  dest=Path(row['path']);data=z.read(row['member']);assert hashlib.sha256(data).hexdigest()==row['sha256']
  if dest.exists():assert hashlib.sha256(dest.read_bytes()).hexdigest()==row['sha256'];continue
  assert shutil.disk_usage(dest.parent).free>=len(data)+128*1024**2
  tmp=dest.with_name(dest.name+'.restore');assert not tmp.exists();tmp.write_bytes(data);tmp.chmod(row['mode']);os.utime(tmp,ns=(row['mtime_ns'],row['mtime_ns']));tmp.rename(dest)
print('Exact historical fixture bytes restored; no process started')
''')
