from pathlib import Path
import hashlib,json,os,shutil,time,urllib.request
root=Path(__file__).resolve().parent;plan=json.loads((root/'asset-plan-01.json').read_text());out=root/'assets-1b7-01';out.mkdir(mode=0o700);start=time.monotonic();result={'revision':plan['revision'],'model':plan['model'],'files':[],'limits':plan['limits'],'model_actions':0};report=root/'asset-result-01.json'
def save():report.write_text(json.dumps(result,indent=2)+'\n')
def bounded():
 if time.monotonic()-start>plan['limits']['seconds']:raise TimeoutError('asset deadline')
 if shutil.disk_usage(root).free<plan['limits']['free_disk_reserve_bytes']:raise RuntimeError('disk reserve')
try:
 for item in plan['files']:
  bounded();name=item['rfilename'];relative=Path(name)
  if relative.is_absolute() or '..' in relative.parts:raise RuntimeError('invalid asset path')
  target=out/relative;target.parent.mkdir(parents=True,exist_ok=True);partial=target.with_name(target.name+'.part')
  url=f'https://huggingface.co/{plan["model"]}/resolve/{plan["revision"]}/{name}?download=true'
  sha=hashlib.sha256();blob=hashlib.sha1(f'blob {item["size"]}\0'.encode());count=0;mark=0
  with urllib.request.urlopen(url,timeout=plan['limits']['read_timeout_seconds']) as response,partial.open('xb') as stream:
   if response.status!=200:raise RuntimeError('full asset response is not 200')
   while True:
    bounded();chunk=response.read(1024*1024)
    if not chunk:break
    count+=len(chunk)
    if count>item['size']:raise RuntimeError('asset exceeds frozen length')
    stream.write(chunk);sha.update(chunk);blob.update(chunk)
    if count-mark>=128*1024*1024:print(name,count,'/',item['size'],flush=True);mark=count
   stream.flush();os.fsync(stream.fileno())
  if count!=item['size']:raise RuntimeError('asset truncated')
  if 'lfs' in item:
   if sha.hexdigest()!=item['lfs']['sha256']:raise RuntimeError('LFS SHA256 mismatch')
  elif blob.hexdigest()!=item['blobId']:raise RuntimeError('Git blob mismatch')
  partial.rename(target);target.chmod(0o444);result['files'].append({'path':name,'bytes':count,'sha256':sha.hexdigest(),'blob':blob.hexdigest(),'url':url});save();print('verified',name,count,flush=True)
 result['status']='PASS_FULL_ASSET_IDENTITY_ONLY'
except BaseException as e:result.update(status='FAILED',error=f'{type(e).__name__}: {e}');raise
finally:result.update(seconds=time.monotonic()-start,free_disk_after=shutil.disk_usage(root).free);save()
