import ast
import hashlib
import json
from pathlib import Path
import runpy
import time
import traceback

root = Path.cwd()
work = root / '.q4t-work/serve-audio-native-20261117'
out = work / 'tls-driver-cpu-01'
out.mkdir()
source = root / 'tools/native_asr/service_acceptance.py'
(out / 'source.py').write_bytes(source.read_bytes())
plan = dict(tests=['exact-cleanup-metric-and-negative-controls',
                   'completed-tls-identity-wire-monitoring-cleanup'],
            seconds=60, gpu_actions=0,
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
(out / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
started = time.monotonic()
result = dict(status='FAILED', tests=[], gpu_actions=0)
try:
    ast.parse(source.read_text())
    driver = runpy.run_path(str(source))
    observation = json.loads((work / 'native-tls-01/execution/before-cancel.json').read_text())[0]
    raw = observation['response']['body']
    values = driver['metric_values'](raw)
    keys = ('q4t_active_chats', 'q4t_structured_cleanup_pending',
            'q4t_structured_workers_active', 'q4t_structured_active',
            'q4t_structured_preparing')
    assert all(values[key] == 0 for key in keys)
    assert values['q4t_seq_slots_free'] == values['q4t_seq_slots_total'] == 1
    assert values['q4t_structured_workers_started_total'] == values['q4t_structured_workers_reaped_total']
    row = 'q4t_structured_cleanup_pending 0'
    assert raw.count(row) == 1
    changes = ['', row + '\n' + row, 'q4t_structured_cleanup_pending NaN',
               'q4t_structured_cleanup_pending -1',
               'q4t_structured_cleanup_pending ' + str(2**64)]
    for replacement in changes:
        try:
            driver['metric_values'](raw.replace(row, replacement))
        except RuntimeError:
            pass
        else:
            raise AssertionError('accepted invalid cleanup gauge: ' + replacement)
    result['tests'].append(dict(name=plan['tests'][0], status='PASS',
                                negative_controls=len(changes)))
    reused = driver['reuse_tls'](work / 'native-tls-01')
    assert reused['counts']['starts'] == 1 and reused['counts']['chats'] == 5
    assert reused['native_gateway_recovered_without_model_restart']
    (out / 'reused-observations.json').write_text(json.dumps(reused, indent=2) + '\n')
    result['tests'].append(dict(name=plan['tests'][1], status='PASS',
                                revalidated_files=len(reused['files']),
                                original_status='FAILED'))
    result['status'] = 'PASS'
except BaseException:
    result['error'] = traceback.format_exc()
result['seconds'] = time.monotonic() - started
if result['seconds'] > plan['seconds']:
    result['status'] = 'FAILED'
(out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
raise SystemExit(0 if result['status'] == 'PASS' else 1)
