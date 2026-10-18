"""Run under gpu_exclusive; restore A only while no service is running."""
from pathlib import Path
import hashlib,json,shutil,os
R=Path('/home/rm01/models/dev/qwen4-thor');O=R/'.q4t-work/b1a-deploy-20261018'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
a=O/'previous/q4t';b=R/'build/q4t';cache=R/'.q4t-work/b1a-20261017/CMakeCache.txt'
assert sha(a)=='8685d8bf4068b81df0f5162d6c55924d93eae793d17664a1b465ca7587d6c6ee'
assert sha(b)=='44abd8b1a7edaca41620b87b4cf894427f8397c1e3529ef56512940ef1b44e03'
assert sha(cache)=='ca2e60a10bb4c6c3d653a73437283e543797d119b5fbd7e9cdc82835af549e06'
meta={'version':'pre-b1a-A-restored','binary_sha256':sha(a),'build_cache':str(cache),'build_cache_sha256':sha(cache),'identity_evidence':str(R/'docs/evidence/no-mtp-calibration-design-20261018/identity.json'),'scope':'restored prior default binary; no new service qualification','historical_stale_sidecar':str(O/'previous/q4t.release.json')}
# Do not reinstate September metadata whose binary digest does not match A.
shutil.copy2(a,R/'build/q4t.rollback-new')
(R/'build/q4t.release.rollback-new.json').write_text(json.dumps(meta,indent=2)+'\n')
os.replace(R/'build/q4t.rollback-new',b)
os.replace(R/'build/q4t.release.rollback-new.json',R/'build/q4t.release.json')
assert sha(b)==sha(a)
print('Restored A and matching identity metadata; service remains stopped.')
