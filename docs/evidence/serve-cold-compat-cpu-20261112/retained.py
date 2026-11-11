from pathlib import Path
import json,sys,time
root=Path('/home/rm01/models/dev/qwen4-thor');sys.path.insert(0,str(root))
from tools.serve_recovery import cold_compat,version_contract,controller
from tools.serve_service import package as e
out=root/'.q4t-work/serve-device-domain-20261112/compat-01'
release=Path('build/service.release.json').resolve();metadata=e.read(release)
source=Path(metadata['package']);digest=metadata['manifest_sha256']
before=e.sha(source/'manifest.json');started=time.monotonic()
result=cold_compat.prepare(source,digest,out/'retained-cold-fallback')
selected=version_contract.payload(dict(package=result['payload'],manifest_sha256=result['payload_manifest_sha256']))
receipt={name:dict(path=str(path),sha256=e.sha(path)) for name,path in [('release',release),('decision',Path(metadata['acceptance_record']))]}
assert version_contract.accepted_fallback(receipt,selected)==receipt
assert cold_compat.aware(result['payload'])
assert e.sha(source/'manifest.json')==before==digest
result.update(accepted_origin_verified=True,origin_unchanged=True,seconds=time.monotonic()-started,
 controller_matches_current=(Path(result['payload'])/cold_compat.SOURCE).read_bytes()==Path(controller.__file__).read_bytes())
e.save(out/'retained.json',result)
print(json.dumps(result,indent=2))
