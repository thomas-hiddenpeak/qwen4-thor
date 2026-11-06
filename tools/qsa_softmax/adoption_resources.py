"""Read-only resource windows for the completed 30-cell HTTP comparison.

No model execution or causal performance attribution. Physical pressure uses
MemTotal minus MemAvailable; RSS is a separate observation, never added to it.
"""
import importlib.util
from pathlib import Path
import re

spec = importlib.util.spec_from_file_location('qsa_adoption_resources',
    Path(__file__).with_name('adoption.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def counters(text):
    return {k: int(v) * 1024 for k, v in
            re.findall(r'^(\w+):\s+(\d+) kB', text, re.M)}


def main():
    plan = a.read(a.PLAN)
    a.base.monitored_stage.require_complete_monitor(plan)
    a.require(a.read(a.PLAN.parent / 'lease/exit.json')['status'] == 'COMPLETED',
              'completed HTTP lease required')
    review = a.read(a.PLAN.parent / 'review.json')
    a.require(review['outputs_identical'], 'completed paired raw review required')
    output = Path(plan['output'])
    windows = []
    for step in plan['steps']:
        out = output / step['name']
        pid = (out / 'server.pid').read_text().strip()
        events = [a.json.loads(line) for line in (out / 'events.jsonl').read_text().splitlines()]
        for case in step['cases'][:-1]:
            bounds = {e['event']: e['monotonic_seconds'] for e in events
                      if e.get('case') == case['id']}
            windows.append(dict(arm=step['arm'], head=step['head'], case=case['id'],
                pid=pid, begin=bounds['client_before'], end=bounds['client_after'],
                samples=0, process_samples=0, minimum_available_bytes=None,
                maximum_total_minus_available_bytes=0, maximum_server_rss_bytes=0,
                maximum_server_swap_bytes=0, clocks={}, temperatures={}, unavailable=[]))
    telemetry = output / 'unified-memory.jsonl'
    a.require(a.sha(telemetry) == a.read(output / 'files.json')['unified-memory.jsonl'],
              'sealed telemetry identity')
    with telemetry.open() as stream:
        for line in stream:
            sample = a.json.loads(line)
            for w in windows:
                if not (w['begin'] <= sample['monotonic'] and
                        sample['sample_finished_monotonic'] <= w['end']):
                    continue
                w['samples'] += 1
                mem = counters(sample['meminfo'])
                available = mem['MemAvailable']
                w['minimum_available_bytes'] = min(
                    w['minimum_available_bytes'] if w['minimum_available_bytes'] is not None else available,
                    available)
                w['maximum_total_minus_available_bytes'] = max(
                    w['maximum_total_minus_available_bytes'], mem['MemTotal'] - available)
                process = sample['processes'].get(w['pid'])
                if process and 'status' in process:
                    w['process_samples'] += 1
                    status = counters(process['status'])
                    for field, key in [('maximum_server_rss_bytes', 'VmRSS'),
                                       ('maximum_server_swap_bytes', 'VmSwap')]:
                        w[field] = max(w[field], status[key])
                for name, value in sample['source_values'].items():
                    kind = ('clocks' if name.endswith(('cur_freq', 'cur_rate'))
                            else 'temperatures' if name.endswith('.temp') else None)
                    if kind is None:
                        continue
                    raw = value.get('raw', '').strip()
                    if value['status'] != 'OK' or not raw.lstrip('-').isdigit():
                        w['unavailable'].append(dict(name=name, status=value['status']))
                        continue
                    n = int(raw)
                    item = w[kind].setdefault(name, dict(minimum=n, maximum=n))
                    item['minimum'] = min(item['minimum'], n)
                    item['maximum'] = max(item['maximum'], n)
    a.require(len(windows) == 30 and all(w['samples'] > 0 and
        w['process_samples'] > 0 and w['clocks'] and w['temperatures'] for w in windows),
        '30 measured resource windows')
    a.save(a.PLAN.parent / 'resources.json', dict(windows=windows,
        review_sha256=a.sha(a.PLAN.parent / 'review.json'),
        telemetry_sha256=a.sha(telemetry), source_sha256=a.sha(Path(__file__)),
        note='Complete samples inside actual client windows. PID-bound server RSS; '
             'not added to physical pressure. Observations cannot prove causality '
             'or exclude interference below the sampling interval.'))
    print('Recorded 30 resource windows; no added GPU work')


if __name__ == '__main__':
    main()
