from pathlib import Path
import gzip,hashlib,json,os,shutil
O=Path(__file__).resolve().parent
for row in json.loads((O/'plan.json').read_text())['files']:
 paths=[Path(p) for p in row['paths']]
 if all(p.exists() for p in paths):
  assert all(hashlib.file_digest(p.open('rb'),'sha256').hexdigest()==row['sha256'] for p in paths)
  continue
 assert all(not p.exists() for p in paths)
 assert hashlib.file_digest(Path(row['archive']).open('rb'),'sha256').hexdigest()==row['archive_sha256']
 tmp=paths[0].with_name(paths[0].name+'.restore')
 with gzip.open(row['archive'],'rb') as src,tmp.open('xb') as dst:shutil.copyfileobj(src,dst)
 assert hashlib.file_digest(tmp.open('rb'),'sha256').hexdigest()==row['sha256']
 tmp.chmod(row['mode']);os.utime(tmp,ns=(row['atime_ns'],row['mtime_ns']));tmp.replace(paths[0])
 for p in paths[1:]:os.link(paths[0],p)
print('Exact retired CPU executables restored; compressed originals remain.')
