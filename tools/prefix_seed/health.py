"""Two seed/readonly requests per installed head, no extra inference probes."""
import importlib.util
import json
from pathlib import Path
import re
import time

spec = importlib.util.spec_from_file_location(
    'prefix_seed_health_common', Path(__file__).with_name('run_stage.py'))
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


def validate(plan):
    if (plan['stage'] != 'PREFIX_SEED_HEALTH' or plan['http_requests'] != 4
            or plan['http_loads'] != 2 or len(plan['steps']) != 2
            or [s['head'] for s in plan['steps']] != ['full', 'subset']
            or any(s['http_requests'] != 2 or s['http_loads'] != 1
                   for s in plan['steps'])):
        raise ValueError('Health scope')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('Task deadline')
    for path in plan['required_gates']:
        if not json.loads(Path(path).read_text())['passed']:
            raise ValueError('Release gate')


def case(plan, step, item, out, env, check):
    if item['id'] != 'installed-prefix-seed':
        raise ValueError('Health case')
    prompt = Path(plan['development_prompt']).read_text()
    scope = common.capability('installed-prefix-seed-' + step['head'])
    first = common.request(plan, out, 'source', prompt, 8,
        dict(prefix_cache_scope=scope, prefix_cache_write_tokens=8192,
             session_id=common.capability('installed-parent')))
    second = common.request(plan, out, 'child', prompt + first['text'] + common.turn(
        'What is the audit_code in the authoritative shared record? Return only its six digits.'),
        16, dict(prefix_cache_scope=scope,
                 session_id=common.capability('installed-child')))
    if (common.cached(first) != 0 or common.cached(second) != 8192
            or second['text'].strip() != '831907'):
        raise RuntimeError('Installed shared restore/quality')
    writes = [dict(re.findall(r'(\w+)=(\S+)', line))
              for line in (out / 'server.log').read_text().splitlines()
              if line.startswith('[prefix-cache] request=')]
    source_id = next(m['id'] for m in first['messages'] if m.get('id'))
    if (len(writes) != 1 or writes[0]['request'] != source_id
            or writes[0]['prefix'] != '8192' or writes[0]['error'] != 'none'):
        raise RuntimeError('Installed seed/readonly policy')
    check()
    result = dict(passed=True, head=step['head'], requests=2, cached_tokens=8192,
                  first=first, second=second, shared_writes=writes)
    common.save(out / 'health-result.json', result)
    return result


def step(plan, name):
    common.multi.case = case
    common.custom_step(dict(plan, stage='PREFIX_D'), name)


if __name__ == '__main__':
    common.reuse.validate = validate
    common.reuse.custom_step = step
    common.reuse.admission.audit_server = common.audit
    common.reuse.main()
