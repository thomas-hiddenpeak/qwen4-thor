"""One new host-contract batch strictly after the admitted quality HTTP."""
import argparse
import re
import time
from observer_common import R,O,run,read,save,sha,frozen,admitted,passed

parser=argparse.ArgumentParser();parser.add_argument('--plan-sha256',required=True);args=parser.parse_args()
plan=frozen(args.plan_sha256);admitted(plan,args.plan_sha256);passed('q01-quality-on',plan,args.plan_sha256)
assert not (R/'observer-contracts-decision.json').exists()
assert not list(R.glob('s0*-controller-start.json'))
records=[]
for row in plan['contract_commands']:
    result=run(row['argv'],R,row['label'],cwd=row['cwd'],timeout=600)
    records.append(result)
    assert result['returncode']==0 and result['failure'] is None and result['cleanup_complete'],row['label']
cpp=(R/'observer-contracts-host-01.log').read_text()
py=(R/'observer-contracts-protocol-01.log').read_text()
assert cpp.count('\nPASS ')+int(cpp.startswith('PASS '))==23 and '23 contracts passed' in cpp
assert re.search(r'Ran 23 tests in ',py) and py.rstrip().endswith('OK')
assert not re.search(r'warning\s*:|warning\s*#',(R/'observer-contracts-build-01.log').read_text(),re.I)
sources={str(R/(r['label']+suffix)):sha(R/(r['label']+suffix)) for r in plan['contract_commands'] for suffix in ('.log','-start.json','-exit.json')}
for p in plan['contract_sources']:sources[p]=sha(p)
frozen(args.plan_sha256)
save(R/'observer-contracts-decision.json',{'schema':1,'passed':True,'plan_sha256':args.plan_sha256,
    'runtime_binary_sha256':plan['runtime_binary_sha256'],'host_contracts':23,'protocol_contracts':23,
    'total':46,'first_batch':True,'source_sha256':sources,'recorded_t':time.time()})
print('46 observer contracts first batch passed',flush=True)
