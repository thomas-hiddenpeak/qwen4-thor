from pathlib import Path
import hashlib,json,os,shutil,tarfile
O=Path(__file__).resolve().parent;plan=json.loads((O/'plan.json').read_text())
sha=lambda p:hashlib.file_digest(Path(p).open('rb'),'sha256').hexdigest()
assert sha(plan['archive'])==plan['archive_sha256']
with tarfile.open(plan['archive'],'r:gz') as src:
 for row in plan['files']:
  p=Path(row['path'])
  if p.exists():assert sha(p)==row['sha256'];continue
  member=src.getmember(row['name']);assert member.isfile()
  tmp=p.with_name(p.name+'.restore');assert not tmp.exists()
  with src.extractfile(member) as source,tmp.open('xb') as out:shutil.copyfileobj(source,out)
  assert sha(tmp)==row['sha256'];tmp.chmod(row['mode']);os.utime(tmp,ns=(row['atime_ns'],row['mtime_ns']));tmp.replace(p)
print('Exact build intermediates restored; archives retained.')
