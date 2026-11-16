"""Verify actual sealed qualifications and unchanged deployment state, CPU only."""
import copy,json,sys
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tools.serve_service import package as p
from tools.serve_recovery import bundle,ledger,profile_manager,profile_migration
W=Path(__file__).resolve().parent
paths=[(ROOT/'build/service').resolve(),W/'retained-01/bundle']
old,new=[bundle.verify(path,p.sha(path/'manifest.json')) for path in paths]
first,second=[profile_manager.evidence(path,manifest) for path,manifest in zip(paths,(old,new))]
before=p.read(ROOT/'build/service-native-state/profile-selection/policy.json')['state']['policy']
grant=profile_manager.compatible(old,new,*paths)
stamp=before['clock'];after=profile_migration.candidate(before,grant,ledger.Stamp(stamp['boot_id'],stamp['monotonic_ns']))
for key in ('history','pending','selected','not_before_ns','light_since_ns','clock','configuration'):
 p.require(before[key]==after[key],'changed protected policy: '+key)
negatives=0
for automatic,reverse in [(True,False),(False,True)]:
 a,b,x,y=(new,old,second,first) if reverse else (old,new,first,second)
 try:profile_migration.permit(a['recovery_profiles'],b['recovery_profiles'],x,y,a['recovery_profile_policy'],b['recovery_profile_policy'],automatic=automatic,allow_capacity_change=True,allow_capability_extension=True)
 except ValueError:negatives+=1
 else:raise AssertionError('lost automatic/feature protection')
p.save(W/'migration-actual-review.json',dict(status='PASS_ACTUAL_CHAT_EFFORT_UPGRADE',old_package_sha256=p.sha(paths[0]/'manifest.json'),new_package_sha256=p.sha(paths[1]/'manifest.json'),permit=grant,protected_policy_preserved=True,negative_controls=negatives,default_unchanged=True,source_sha256=p.sha(ROOT/'tools/serve_recovery/profile_migration.py'),new_gpu_starts=0))
print(json.dumps(dict(status='PASS_ACTUAL_CHAT_EFFORT_UPGRADE',negative_controls=negatives)))
