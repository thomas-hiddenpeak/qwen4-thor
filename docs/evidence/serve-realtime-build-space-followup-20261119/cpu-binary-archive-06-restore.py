from pathlib import Path
import json,hashlib,tarfile,os,sys,shutil
w=Path(__file__).resolve().parent;p=json.loads((w/'plan.json').read_bytes());r=json.loads((w/'result.json').read_bytes());a=w/'payloads.tar.gz'
assert hashlib.sha256(a.read_bytes()).hexdigest()==r['archive_sha256']
selected=set(sys.argv[1:]);known={row['path'] for row in p['files']}
assert selected and selected<=known,'Pass exact original absolute paths from plan.json'
with tarfile.open(a) as t:
 for row in p['files']:
  if row['path'] not in selected:continue
  dest=Path(row['path']);data=t.extractfile(row['member']).read();assert hashlib.sha256(data).hexdigest()==row['sha256']
  if dest.exists():assert hashlib.sha256(dest.read_bytes()).hexdigest()==row['sha256'];continue
  assert shutil.disk_usage(dest.parent).free>=row['bytes']+(128<<20)
  tmp=dest.with_name(dest.name+'.restore');assert not tmp.exists();tmp.write_bytes(data);tmp.chmod(row['mode']);os.utime(tmp,ns=(row['mtime_ns'],row['mtime_ns']));tmp.rename(dest)
print('Exact historical fixture payloads restored; no process started')
