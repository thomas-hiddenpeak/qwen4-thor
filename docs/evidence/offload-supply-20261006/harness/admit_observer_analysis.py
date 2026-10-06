"""Admit the pre-reviewed arithmetic only after all fixed evidence closes."""
import json
from pathlib import Path
import time
from observer_common import R,frozen,passed,read,save,sha
EXPECTED='907111ee1b731521c286e90df3718457c11c25c340a3f66fabc90a03a7584b24'
plan=frozen(EXPECTED)
review_path=R/'observer-analysis-static-review.json'
review=read(review_path)
assert review['passed'] is True
script=R/'analyze_supply_observer.py'
assert sha(script)=='0f0bda9217b6a6ec384ce50c94a04f6df949133cc98b66ee6597d7fee3f68574'
sources={}
def bind(path):
 path=Path(path).resolve();sources[str(path)]=sha(path);return read(path)
for name in ['observer-analysis-static-review.json','observer-execution-plan.json',
             'observer-execution-admission.json','observer-resource-independent-static-review.json']:
 bind(R/name)
for group in plan['group_ids']:
 passed(group,plan,EXPECTED);bind(R/(group+'-decision.json'))
 for label in [group+'-controller',group+'-audit-01']:
  owner=bind(R/(label+'-exit.json'))
  assert owner['returncode']==0 and owner['failure'] is None and owner['cleanup_complete']
contracts=bind(R/'observer-contracts-decision.json')
assert contracts['passed'] and contracts['total']==46 and contracts['plan_sha256']==EXPECTED
for row in plan['contract_commands']:
 owner=bind(R/(row['label']+'-exit.json'))
 assert owner['returncode']==0 and owner['failure'] is None and owner['cleanup_complete']
resource_owner=bind(R/'observer-resource-audit-01-exit.json')
assert resource_owner['returncode']==0 and resource_owner['failure'] is None and resource_owner['cleanup_complete']
resources=bind(R/'observer-resources.json')
assert resources['audit_complete'] and resources['plan_sha256']==EXPECTED
assert resources['http_request_count']==27 and resources['client_envelope_count']==17
for path,item in resources['metadata_source_sha256'].items():assert sha(path)==item['sha256'],path
for path,item in resources['source_sha256'].items():
 stat=Path(path).stat();signature=(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns)
 assert item['complete_file'] and tuple(item['stat_before'])==tuple(item['stat_after'])==signature,path
sources[str(script)]=sha(script);sources[str(Path(__file__).resolve())]=sha(__file__)
save(R/'observer-analysis-admission.json',{'schema':1,'execution_admitted':True,'plan_sha256':EXPECTED,
 'analysis_script_sha256':sha(script),'runtime_binary_sha256':plan['runtime_binary_sha256'],
 'source_sha256':sources,'recorded_t':time.time(),'raw_integrity_scope':'Completed-reader SHA plus unchanged metadata signature; no raw resource replay.'})
print('closed-scope observer arithmetic admitted',flush=True)
