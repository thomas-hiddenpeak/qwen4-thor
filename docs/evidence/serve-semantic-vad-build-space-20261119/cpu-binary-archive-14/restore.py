from pathlib import Path
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
