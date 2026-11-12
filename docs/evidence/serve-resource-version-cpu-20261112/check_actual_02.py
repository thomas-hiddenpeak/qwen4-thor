import ast
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_recovery import bundle, resource_compat
original=Path('/home/rm01/models/dev/qwen4-thor/.q4t-work/serve-version-recovery-20261112/business-03/retained-fallback')
digest='10155da318447ff4a14115b341e8d7591cf026fcff21484781f1fd4f465408a0'
output=ROOT/'.q4t-work/serve-resource-version-20261112/actual-adapter-01'
before=bundle.verify(original,digest)
result=resource_compat.verify(original,digest,output,bundle.engine.sha(output/'manifest.json'))
assert before==bundle.verify(original,digest)
verified=resource_compat.verify(original,digest,output,result['payload_manifest_sha256'])
assert result==verified
assert result['business_acceptance'] is False and result['model_actions'] is False
assert before['version']==12
assert set(before['files'])==set(bundle.verify(output,result['payload_manifest_sha256'])['files'])
result.update(status='PASS_SOURCE_ADAPTATION_ONLY',original_version=before['version'],original_unchanged=True,historical_inventory_unchanged=True,model_gpu_starts=0)
print(json.dumps(result,sort_keys=True,indent=2))
