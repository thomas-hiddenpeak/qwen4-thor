"""One deterministic measurement-plan correction; preserve total resources."""
import copy
import hashlib
import json
from pathlib import Path
import sys

from run_stage import ROOT, save


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()


def main():
    from transformers import AutoTokenizer
    base=ROOT/'.q4t-work/session-cache-20261027'
    original=base/'phase-c'
    target=base/'phase-c-fixed-cap';target.mkdir()
    plan=copy.deepcopy(json.loads((original/'plan.json').read_text()))
    tokenizer=AutoTokenizer.from_pretrained(plan['model'],local_files_only=True)
    checks=[]
    for scenario in plan['scenarios']:
        gold=json.loads(Path(scenario['reference']).read_text())[0]['text']
        prefix=tokenizer.decode(tokenizer.encode(gold,add_special_tokens=False)[:256],
                                skip_special_tokens=True)
        for case in scenario['second']:
            case['tokens']=256
            case['expected_sha256']=hashlib.sha256(prefix.encode()).hexdigest()
        if scenario['id']=='repeat-code':
            for slot in range(4):
                path=original/'run/00-full-off/repeat-code-second'/str(slot)/'response.json'
                r=json.loads(path.read_text())
                if not r['success'] or not r['text'].startswith(prefix):
                    raise RuntimeError('observed baseline lacks frozen 256-token prefix')
                checks.append(dict(slot=slot,original_tokens=r['output_tokens'],
                    original_finish=r['finish'],prefix_bytes=len(prefix.encode()),
                    prefix_sha256=case['expected_sha256'],passed=True,
                    response_sha256=sha(path)))
    gate=target/'cpu-prefix-gate.json'
    save(gate,dict(passed=True,checks=checks,standard='EVALUATION output cap 256',
         inference_run=False,parameter_scan=False))
    plan.update(output=str(target/'run'),http_requests=90,http_loads=4,
        fixed_cap_correction=True,required_gates=plan['required_gates']+[str(gate)],
        original_plan=str(original/'plan.json'),original_plan_sha256=sha(original/'plan.json'),
        reused_code_groups={r:str(original/'run/00-full-off'/('repeat-code-'+r)/'result.json')
                            for r in ['first','interference']},
        revision=dict(reason='OFF baseline naturally stops before 2048; equal-output precondition invalid',
            original_requests_charged=12,original_groups_reused_requests=8,
            affected_second_round_retest_requests=4,first_cap=2048,second_cap=256,
            previous_self_imposed_fix_count=2,measurement_corrections=1,
            further_corrections_allowed=0,total_http_cap=240,total_starts_cap=14,
            total_gpu_fixtures_cap=4,original_deadline_unchanged=True,
            startup_allocation='A2 + originalC1 + remainingC4 + protection6 + post-install health1 =14',
            projected_requests='A35 + originalC12 + remainingC90 + protection94 + health2 =233',
            health_reuse='Subset identical binary/artifact exercise reused from protection; full post-install health retained.'),
        design='Same first/interference/new second prompts, fixed second max_tokens 256; baseline code first/interference reused. No warmup or cache drop. One descriptive pair per head/scenario; code OFF second uses fresh restart, explicitly recorded.')
    path=target/'plan.json'
    for index,step in enumerate(plan['steps']):
        if index==0:
            step['name']='00-full-off-remaining'
            step['http_requests']=16
            step['reuse_code_prefix']=True
        step['command']=[sys.executable,'-B',str(ROOT/'tools/session_cache/run_performance_resume.py'),
                         '--plan',str(path),'--custom-step',step['name']]
    for name in plan['files']:plan['files'][name]=sha(name)
    extra=[gate,ROOT/'tools/session_cache/run_performance_resume.py',
           ROOT/'tools/session_cache/prepare_fixed_cap.py',original/'archive/audit.json',
           *[Path(p) for p in plan['reused_code_groups'].values()]]
    for p in extra:plan['files'][str(p)]=sha(p)
    save(path,plan)
    print(json.dumps(dict(plan=str(path),sha256=sha(path),cpu_gate=json.loads(gate.read_text()),
                          revision=plan['revision']),indent=2))


if __name__=='__main__':main()
