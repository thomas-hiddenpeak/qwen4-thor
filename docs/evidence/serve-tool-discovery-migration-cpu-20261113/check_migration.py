from pathlib import Path
import sys
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_recovery import bundle, profile_manager, version_contract
from tools.serve_service import package as p
W=Path(__file__).resolve().parent
paths=[(ROOT/'build/service').resolve(), W/'candidate-01/bundle', W/'retained-01/bundle']
values=[version_contract.payload(dict(package=str(x),manifest_sha256=p.sha(x/'manifest.json'))) for x in paths]
a,b,c=values
extension=profile_manager.compatible(a['manifest'],b['manifest'],paths[0],paths[1])
automatic=version_contract.compatible(b,c)
assert extension['format']=='q4t-profile-migration-permit-v3' and not extension['automatic']
assert automatic['format']=='q4t-profile-migration-permit-v1' and automatic['automatic']
result=dict(status='PASS_ACTUAL_QUALIFIED_DISCOVERY_MIGRATION',explicit_extension=extension,automatic_recovery=automatic,
 descriptors=[v['descriptor'] for v in values],source_sha256=p.sha(ROOT/'tools/serve_recovery/profile_migration.py'),
 new_gpu_starts=0,default_changed=False)
p.save(W/'migration-actual.json',result)
print(result['status'])
