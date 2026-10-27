"""One fixed-cap correction after baseline EOS; no parameter search/retry."""
import json
import time
from pathlib import Path

import run_performance as common
from run_stage import reuse, save, legacy
from audit import audit_server

original_group = common.group


def group(plan, step, scenario, round_name, out, env, check):
    if (step.get('reuse_code_prefix') and scenario['id']=='repeat-code' and
            round_name in ['first','interference']):
        source=Path(plan['reused_code_groups'][round_name])
        if legacy.sha(source)!=plan['files'][str(source)]:
            raise RuntimeError('reused group changed')
        result=json.loads(source.read_text())
        result['reused_from']=str(source)
        directory=out/(scenario['id']+'-'+round_name);directory.mkdir()
        save(directory/'result.json',result)
        return result
    return original_group(plan,step,scenario,round_name,out,env,check)


def validate(plan):
    if (plan['stage']!='SESSION_C' or not plan['fixed_cap_correction'] or
            (plan['http_requests'],plan['http_loads'])!=(90,4) or
            sum(s['http_requests'] for s in plan['steps'])!=90 or
            len(plan['steps'])!=4):
        raise ValueError('corrected C remaining-only ledger')
    if time.time()>=plan['task_deadline_unix']:
        raise TimeoutError('original absolute deadline')
    for scenario in plan['scenarios']:
        if ([c['tokens'] for c in scenario['first']]!=[2048]*4 or
                [c['tokens'] for c in scenario['second']]!=[256]*4):
            raise ValueError('frozen first/second caps')
    for gate in plan['required_gates']:
        if not json.loads(Path(gate).read_text())['passed']:
            raise ValueError('unpassed prerequisite')


if __name__=='__main__':
    common.group=group
    reuse.validate=validate
    reuse.custom_step=common.custom_step
    reuse.admission.audit_server=audit_server
    reuse.main()
