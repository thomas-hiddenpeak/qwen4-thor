"""Apply the frozen eight-request interaction gate; never infer from partial rows."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def interaction(rows):
    assert len(rows) == 8
    cells = {}
    for r in rows:
        cells.setdefault((r['variant'], r['mode']), []).append(r['metrics']['ttft'])
    assert len(cells) == 4 and all(len(v) == 2 for v in cells.values())
    radius = max(abs(v[1] - v[0]) for v in cells.values())
    halves = []
    for half in (rows[:4], rows[4:]):
        values = {(r['variant'], r['mode']): r['metrics']['ttft'] for r in half}
        original = values['B', 'original'] - values['A', 'original']
        shared = values['B', 'shared'] - values['A', 'shared']
        delta = original - shared
        halves.append({'D_original_ms': original * 1000,
                       'D_shared_ms': shared * 1000,
                       'interaction_ms': delta * 1000,
                       'positive_control': original > 0,
                       'interaction_exceeds_2R': delta > 2 * radius,
                       'shared_residual_within_R': abs(shared) <= radius})
    passed = all(all(h[k] for k in ('positive_control', 'interaction_exceeds_2R',
                                   'shared_residual_within_R')) for h in halves)
    return {'R_ms': radius * 1000, 'threshold_2R_ms': radius * 2000,
            'halves': halves, 'frozen_interaction_gate_passed': passed}


def main():
    protocol = json.loads((ROOT / 'protocol.json').read_text())
    lease = json.loads((ROOT / 'lease/exit.json').read_text())
    assert lease['status'] == 'COMPLETED' and lease['child_returncode'] == 0
    for path, sha in protocol['sha256'].items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha, path
    assert hashlib.sha256((ROOT / 'run.py').read_bytes()).hexdigest() == protocol['driver_sha256']
    monitor_count = 0
    for line in (ROOT / 'lease/events.jsonl').read_text().splitlines():
        event = json.loads(line)
        if event['event'] == 'monitor':
            monitor_count += 1
            assert event['conflicts'] == [] and event['unreadable_processes'] == 0
    assert monitor_count > 0
    driver = (ROOT / 'driver.log').read_text()
    assert driver.count('[persistent-owner] cleanup_complete=3 driver_exit=0') == 1
    assert 'Eight frozen persistent-placement requests completed.' in driver
    rows = json.loads((ROOT / 'observations.json').read_text())
    assert len(rows) == protocol['requests'] == 8
    for index, (row, condition) in enumerate(zip(rows, protocol['order'])):
        assert [row['variant'], row['mode']] == condition
        name = f'{index + 1:02}-{condition[0]}-{condition[1]}'
        assert row['name'] == name
        dest = ROOT / 'http' / name
        exit_record = json.loads((dest / 'exit.json').read_text())
        assert exit_record['http_output_checks_passed'] and exit_record['server'] == 0
        assert exit_record['diagnostic_persistent_backing_fds']
        assert json.loads((ROOT / f'{name}-exit.json').read_text())['returncode'] == 0
        result = json.loads((dest / 'results.json').read_text())
        assert len(result) == 1 and result[0]['length'] == 204800
        assert result[0]['outputs'] == row['outputs'] == [protocol['expected_output']]
        assert result[0]['metrics'][0] == row['metrics']
        assert result[0]['prompt_sha256'] == row['prompt_sha256']
        text = (dest / 'server.log').read_text()
        assert 'INVALID' not in text
        assert text.count('[persistent-layout] startup_complete=1541') == 1
        assert text.count('[persistent-layout] cleanup_complete=3') == 1
    report = interaction(rows)
    report.update(validity_passed=True, guard_monitor_records=monitor_count,
                  scope='joint persistent placement under this diagnostic allocation regime',
                  production_acceptance=False, root_cause_proven=False,
                  next_action=('decompose VA/backing/cache history with a separate frozen protocol'
                               if report['frozen_interaction_gate_passed'] else
                               'stop this placement family; no extra requests or threshold changes'))
    (ROOT / 'interaction-review.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))

if __name__ == '__main__':
    main()
