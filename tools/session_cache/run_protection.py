"""Five-tier protection plus two-turn recall, within the original total cap."""
import json
import time
import hashlib
from pathlib import Path

import run_performance as common
from run_stage import reuse, request, save
from audit import audit_server

original_case = common.multi.case


def case(plan, step, c, out, env, check):
    if c['id'] != 'session-memory':
        return original_case(plan, step, c, out, env, check)
    if not step['cache'] or step['head'] == 'plain':
        return dict(id=c['id'], skipped=True, requests=0)
    key=hashlib.sha256(('memory-quality-'+step['head']).encode()).hexdigest()
    prompt=Path(c['first']).read_text()
    first=request(plan,out,'memory-first',prompt,16,key)
    if first['text'].strip()!='7':
        raise RuntimeError('first recall turn exact answer')
    second_prompt=prompt+first['text']+c['append']
    second=request(plan,out,'memory-second',second_prompt,16,key)
    if (second['text'].strip()!=c['expected'] or
            second['usage']['prompt_tokens_details']['cached_tokens']<40000):
        raise RuntimeError('cached long-history recall failed')
    result=dict(id=c['id'],passed=True,requests=2,expected=c['expected'],
        cached_tokens=second['usage']['prompt_tokens_details']['cached_tokens'],
        first=first,second=second)
    save(out/'memory-quality.json',result)
    check()
    return result


def validate(plan):
    if (plan['stage']!='SESSION_D' or plan['http_requests']!=94 or
            plan['http_loads']!=6 or len(plan['steps'])!=6 or
            sum(s['http_requests'] for s in plan['steps'])!=94):
        raise ValueError('fixed protection/recall ledger')
    if time.time()>=plan['task_deadline_unix']:
        raise TimeoutError('absolute task deadline')
    for path in plan['required_gates']:
        if not json.loads(Path(path).read_text())['passed']:
            raise ValueError('unpassed prerequisite')


if __name__=='__main__':
    common.multi.case=case
    reuse.validate=validate
    reuse.custom_step=common.custom_step
    reuse.admission.audit_server=audit_server
    reuse.main()
