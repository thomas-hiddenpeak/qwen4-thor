"""Two-request post-install health; reuses accepted subset applicability."""
import hashlib
import json
from pathlib import Path
import time

import run_performance as common
from run_stage import reuse, request, save
from audit import audit_server


def case(plan, step, item, out, env, check):
    if item['id'] != 'installed-session' or step['head'] != 'full':
        raise ValueError('health scope changed')
    key = hashlib.sha256(b'installed-session-health-20261027').hexdigest()
    prompt = Path(plan['session_prompt']).read_text()
    first = request(plan, out, 'installed-first', prompt, 8, key)
    if not first['text'] or first['usage']['prompt_tokens_details']['cached_tokens']:
        raise RuntimeError('first health request contract')
    resumed = prompt + first['text'] + (
        '<|im_end|>\n<|im_start|>user\nIgnore the previous task. '
        'Reply with exactly the number 43 and nothing else.<|im_end|>\n'
        '<|im_start|>assistant\n<think>\n\n</think>\n\n')
    second = request(plan, out, 'installed-resume', resumed, 16, key)
    prefix = second['usage']['prompt_tokens_details']['cached_tokens']
    if second['text'].strip() != '43' or prefix < 8192:
        raise RuntimeError('installed restore/quality failed')
    check()
    result = dict(passed=True, requests=2, cached_tokens=prefix,
                  first=first, second=second)
    save(out / 'health-result.json', result)
    return result


def validate(plan):
    if (plan['stage'] != 'SESSION_HEALTH' or plan['http_requests'] != 2 or
            plan['http_loads'] != 1 or len(plan['steps']) != 1 or
            plan['steps'][0]['http_requests'] != 2):
        raise ValueError('health cap')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('original absolute deadline')
    for path in plan['required_gates']:
        if not json.loads(Path(path).read_text())['passed']:
            raise ValueError('unpassed release prerequisite')


if __name__ == '__main__':
    common.multi.case = case
    reuse.validate = validate
    reuse.custom_step = common.custom_step
    reuse.admission.audit_server = audit_server
    reuse.main()
