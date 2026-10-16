"""Analyze only the frozen four observations after the GPU lease terminates."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
protocol = json.loads((root / 'protocol.json').read_text())
lease = json.loads((root / 'lease/exit.json').read_text())
assert lease['status'] == 'COMPLETED' and lease['child_returncode'] == 0
monitor = [json.loads(x) for x in (root / 'lease/events.jsonl').read_text().splitlines()]
assert not any(r.get('conflicts') for r in monitor)
rows = []
for index, variant in enumerate(protocol['order']):
    name = f'{index+1:02}-{variant}'
    folder = root / 'http' / name
    exit_info = json.loads((folder / 'exit.json').read_text())
    assert exit_info['http_output_checks_passed'] and exit_info['server'] == 0
    assert exit_info['diagnostic_ucf'] and not exit_info['performance_acceptance']
    assert (folder / 'binary.sha256').read_text().strip() == protocol['sha256'][variant]
    result = json.loads((folder / 'results.json').read_text())[0]
    assert result['deterministic']
    counters = list(folder.rglob('ucf/counter.json')); assert len(counters) == 1
    counter = json.loads(counters[0].read_text())
    assert counter['complete'] and counter['error'] is None
    b = counter['boundary']
    assert all(0 <= b[k] <= 1000000 for k in ('pre_request_count_ns', 'first_to_stop_ns'))
    snap = counter['rows'][-1]
    assert snap['valid'] and snap['enabled_ns'] == snap['running_ns'] > 0
    assert [x['config'] for x in snap['events']] == [4294967296, 0, 6, 12]
    assert len({x['id'] for x in snap['events']}) == 4
    output = result['outputs'][0]
    rows.append({'name': name, 'variant': variant,
                 'metrics': result['metrics'][0], 'boundary': b,
                 'raw': {str(x['config']): x['value'] for x in snap['events']},
                 'enabled_ns': snap['enabled_ns'],
                 'prompt': result['prompt_sha256'],
                 'output_sha256': hashlib.sha256(output.encode()).hexdigest()})
assert len({r['output_sha256'] for r in rows}) == 1
reference = json.loads((Path('/home/rm01/models/dev/qwen4-thor') /
                        protocol['fixture'] / 'results.json').read_text())
reference_row = next(r for r in reference if r['length'] == 204800)
assert all(hashlib.sha256(text.encode()).hexdigest() == rows[0]['output_sha256']
           for text in reference_row['outputs'])
assert all(r['prompt'] == rows[0]['prompt'] for r in rows)
values = [r['raw']['0'] for r in rows]
r = max(abs(values[3]-values[0]), abs(values[2]-values[1]))
differences = [values[1]-values[0], values[2]-values[3]]
review = {'rows': rows, 'read_byte_pair_differences': differences,
          'read_byte_repeat_range': r, 'threshold': max(r, 1024**3),
          'frozen_gate_passed': all(d > max(r, 1024**3) for d in differences),
          'ttft_pair_differences_s': [rows[1]['metrics']['ttft']-rows[0]['metrics']['ttft'],
                                      rows[2]['metrics']['ttft']-rows[3]['metrics']['ttft']],
          'guard_monitor_samples': sum(x.get('event') == 'monitor' for x in monitor),
          'root_cause_proven': False, 'performance_acceptance': False,
          'decision': 'stop this observation family; no extra samples or post-hoc gate'}
(root / 'review.json').write_text(json.dumps(review, indent=2)+'\n')
print(json.dumps({k:v for k,v in review.items() if k != 'rows'}, indent=2))
