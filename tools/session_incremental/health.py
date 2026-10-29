"""Installed, uninstrumented private/shared cache health: three requests/head."""
import argparse
import importlib.util
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('incremental_health_common', ROOT/'tools/prefix_seed/run_stage.py')
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


def validate(plan):
    if (plan['stage'] != 'INCREMENTAL_HEALTH' or plan['http_requests'] != 6
            or plan['http_loads'] != 2 or len(plan['steps']) != 2
            or [s['head'] for s in plan['steps']] != ['full', 'subset']
            or any(s['http_requests'] != 3 or s['http_loads'] != 1 for s in plan['steps'])):
        raise ValueError('frozen health scope')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('absolute delivery deadline')
    if not json.loads(Path(plan['required_gate']).read_text())['passed']:
        raise ValueError('acceptance required')


def case(plan, step, item, out, env, check):
    prompt = Path(plan['development_prompt']).read_text()
    scope = common.capability('incremental-health-' + step['head'])
    session = common.capability('incremental-health-parent-' + step['head'])
    first = common.request(plan, out, 'source', prompt, 8,
        dict(prefix_cache_scope=scope, prefix_cache_write_tokens=8192, session_id=session))
    second = common.request(plan, out, 'child', prompt + first['text'] + common.turn(
        'What is the audit_code in the authoritative shared record? Return only its six digits.'),
        16, dict(prefix_cache_scope=scope, session_id=common.capability('incremental-health-child')))
    third = common.request(plan, out, 'resume', prompt + first['text'] + common.turn(
        'Reply with exactly 44.'), 16, dict(session_id=session, prefix_cache_scope=scope))
    if (common.cached(first) != 0 or common.cached(second) != 8192
            or second['text'].strip() != '831907' or common.cached(third) <= 8192
            or third['text'].strip() != '44'):
        raise RuntimeError('installed full/shared/private state or output')
    if '[session-save-timing]' in (out/'server.log').read_text():
        raise RuntimeError('health must exercise uninstrumented delivery')
    check()
    result = dict(passed=True, head=step['head'], requests=3, first=first, second=second, third=third)
    common.save(out/'health-result.json', result)
    return result


def step(plan, name):
    common.multi.case = case
    original = common.legacy.clean_env
    common.legacy.clean_env = lambda: original() | {'Q4T_SESSION_INCREMENTAL': '1'}
    common.custom_step(dict(plan, stage='PREFIX_D'), name)


if __name__ == '__main__':
    common.reuse.validate = validate
    common.reuse.custom_step = step
    common.reuse.admission.audit_server = common.audit
    common.reuse.main()
