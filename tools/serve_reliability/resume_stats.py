"""Audit one parser-only failure offline; schedule only the unrun timing arm."""
import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/serve-reliability-20261030'
sys.path.insert(0, str(ROOT/'tools/evalscope'))
from serve_reliability import decoded, fields, save, sha


def main():
    plan = json.loads((BASE/'http-plan.json').read_text())
    original = BASE/'frozen/http-runner-original.py'
    runner = ROOT/'tools/evalscope/serve_reliability.py'
    assert sha(original) == plan['files'][str(runner)]
    for path, digest in plan['files'].items():
        assert sha(original if path == str(runner) else path) == digest
    failure = json.loads((BASE/'http/failure.json').read_text())
    guard = json.loads((BASE/'lease/exit.json').read_text())
    assert failure['error'] == "AssertionError('concurrent interval/timing records')"
    assert guard['status'] == 'COMMAND_FAILED' and guard['child_returncode'] == 1
    assert not guard.get('cleanup_error')
    assert (failure['http'], failure['starts']) == (88, 17)
    assert failure['completed'] == [s['name'] for s in plan['steps'][:16]]
    folder = BASE/'http/stats-0'
    exit_record = json.loads((folder/'exit.json').read_text())
    assert exit_record['server_exit'] == 0 and exit_record['lifecycle']
    assert exit_record['requests'] == 4 and exit_record['failure'] == failure['error']
    raw = (folder/'server.log').read_text()
    counts = fields(raw, '[q4t][residency] id=')
    assert len(counts) == 4 and sorted(int(r['interval']) for r in counts) == [1, 2, 3, 4]
    assert not fields(raw, '[q4t][residency][timing] ')
    assert all(r['scope'] == 'global_interval' for r in counts)
    for row in counts:
        for key, value in row.items():
            if key not in ['id', 'scope', 'finish']:
                assert 0 <= float(value) < 1e12
    rows = []
    for i in range(4):
        response = json.loads((folder/f'stats-0-{i}-response.json').read_text())
        row = decoded(response)
        assert row['text'].strip() == plan['quality']['1024']['expected']
        assert row['usage']['prompt_tokens'] == 1024
        rows.append(row)
    resources = json.loads((folder/'resources.json').read_text())
    swaps = [r['process']['process_swap_kb'] for r in resources['samples']
             if r.get('process', {}).get('process_swap_kb') is not None]
    assert swaps and max(swaps)==0 and not resources['errors']
    assert 'shutdown complete (0 in-flight remaining)' in raw
    receipt = BASE/'stats-0-offline-audit.json'
    assert not receipt.exists()
    save(receipt, dict(passed=True, reason='startup policy banners included in request count',
        original_failure=failure, original_exit=exit_record, counts=counts, rows=rows,
        original_tool_sha256=sha(original), repaired_tool_sha256=sha(runner),
        files={str(p):sha(p) for p in folder.rglob('*') if p.is_file()},
        new_http=0, new_starts=0, original_records_unchanged=True))
    remaining = copy.deepcopy(plan)
    remaining.update(output=str(BASE/'http-remaining'), steps=[plan['steps'][-1]],
        planned_measurement_http=4, planned_measurement_starts=1,
        preceding_http=88, preceding_starts=17,
        parent_plan_sha256=sha(BASE/'http-plan.json'),
        amendment='Parser-only repair; preserve all 88 executed HTTP, run timing ON only.')
    remaining['files'][str(runner)] = sha(runner)
    for path in [original, receipt, Path(__file__)]:
        remaining['files'][str(path)] = sha(path)
    assert not (BASE/'http-remaining-plan.json').exists()
    save(BASE/'http-remaining-plan.json', remaining)
    print(json.dumps(dict(offline_pass=True, remaining_http=4, remaining_starts=1,
                         total_planned_http_including_health=98)))


if __name__ == '__main__':
    main()
