"""Audit the frozen HTTP evidence before a serving release can be packaged."""
import json
import shutil
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'tools/evalscope'))
from serve_reliability import fields, sha, save

BASE = ROOT/'.q4t-work/serve-reliability-20261030'


def main():
    plan = json.loads((BASE/'http-plan.json').read_text())
    run = Path(plan['output'])
    remaining_plan = json.loads((BASE/'http-remaining-plan.json').read_text())
    remaining = Path(remaining_plan['output'])
    result = json.loads((remaining/'result.json').read_text())
    guard = json.loads((BASE/'lease/exit.json').read_text())
    assert guard['status'] == 'COMMAND_FAILED' and guard['child_returncode'] == 1
    assert not guard.get('cleanup_error')
    final_guard = json.loads((BASE/'remaining-lease/exit.json').read_text())
    assert final_guard['status'] == 'COMPLETED' and final_guard['child_returncode'] == 0
    offline = json.loads((BASE/'stats-0-offline-audit.json').read_text())
    assert offline['passed'] and offline['new_http'] == offline['new_starts'] == 0
    assert offline['original_failure']['http'] == 88
    assert offline['original_failure']['starts'] == 17
    assert offline['original_failure']['completed'] == [s['name'] for s in plan['steps'][:16]]
    assert result['passed'] and result['http'] == 4 and result['starts'] == 1
    assert result['completed'] == ['stats-1']
    relocated = {str(ROOT/'tools/evalscope/serve_reliability.py'):
                 str(BASE/'frozen/http-runner-original.py')}
    for path, digest in plan['files'].items():
        assert sha(relocated.get(path, path)) == digest, path
    for path, digest in (remaining_plan['files'] | offline['files']).items():
        assert sha(path) == digest, path
    ledger = [json.loads(line) for line in (run/'ledger.jsonl').read_text().splitlines()]
    ledger += [json.loads(line) for line in (remaining/'ledger.jsonl').read_text().splitlines()]
    assert sum(r['kind']=='generation' for r in ledger) == 92
    assert sum(r['kind']=='startup' for r in ledger) == 18
    assert all(r['time'] < plan['deadline_unix'] for r in ledger)
    reviewed = {}
    for step in plan['steps']:
        folder = (remaining if step['name']=='stats-1' else run)/step['name']
        exit_record = json.loads((folder/'exit.json').read_text())
        if step['name'] == 'stats-0':
            assert exit_record == offline['original_exit']
        else:
            assert exit_record['passed']
        assert exit_record['requests'] == step['requests']
        raw = (folder/'server.log').read_text()
        reservation = [json.loads(line.split('] ', 1)[1]) for line in raw.splitlines()
                       if line.startswith('[startup-reservation] ')]
        final = [r for r in reservation if r['event']=='finalized']
        assert len(final) == 1 and final[0]['status'] == 'PASS'
        assert final[0]['unhanded_remaining'] == final[0]['handed_live'] == 0
        ends = [r for r in fields(raw, '[q4t][request] ') if r['event']=='end']
        if step['kind'] == 'fault':
            marker = 'scheduler exception:' if step['exit'] else 'request exception:'
            assert raw.count(marker) == 1
        if step.get('slow'):
            assert raw.count('observed=write.backpressure') == 1
            for suffix in ['slow', 'explicit', 'deadline', 'encode']:
                row = next(r for r in ends if r['id'] == step['name']+'-'+suffix)
                assert row['outcome'] == 'cancelled', row
        if step['kind'] == 'cache':
            failed_id = step['name']+'-failed-send'
            for prefix in ['[session-cache] ', '[prefix-cache] ']:
                rows = [r for r in fields(raw, prefix) if r.get('request') == failed_id
                        and (prefix.startswith('[prefix') or r.get('event') == 'save')]
                assert len(rows) == 1 and rows[0]['error'] == 'none', rows
        reviewed[step['name']] = dict(exit=exit_record, endings=ends,
                                     reservation_final=final[0])
    host = (BASE/'phase4-host/run.log').read_text()
    assert '70 tests, 69 passed, 1 failed, 0 skipped' in host
    assert '[FAIL] server_http_exception_terminal_states' in host
    assert '1 tests, 1 passed, 0 failed, 0 skipped' in (BASE/'phase4-host-repair.log').read_text()
    assert '4 tests, 4 passed, 0 failed, 0 skipped' in (BASE/'phase4-http-lifecycle-hooks.log').read_text()
    assert '\nOK\n' in (BASE/'http-client-tests.log').read_text()
    for name in ['build-final.log', 'build-fault.log', 'build-lifecycle-final.log']:
        text = (BASE/name).read_text()
        assert 'warning:' not in text and 'error:' not in text
    comparisons = []
    for mode in ['plain', 'full', 'subset']:
        before = json.loads((run/('parent-'+mode)/'quality.json').read_text())
        after = json.loads((run/('candidate-'+mode)/'quality.json').read_text())
        for parent, candidate in zip(before, after):
            assert all(parent[k] == candidate[k] for k in ['text', 'usage', 'finish'])
            comparisons.append(dict(mode=mode, length=parent['length'],
                stream=parent['stream'], parent_seconds=parent['seconds'],
                candidate_seconds=candidate['seconds'], parent_ttft=parent['ttft'],
                candidate_ttft=candidate['ttft']))
    evidence = {}
    repaired = BASE/'frozen/http-runner-repaired.py'
    assert not repaired.exists()
    shutil.copy2(ROOT/'tools/evalscope/serve_reliability.py', repaired)
    for folder in [run, remaining, BASE/'lease', BASE/'remaining-lease',
                   BASE/'phase4-host', BASE/'phase3-tokenizer-diff']:
        evidence.update({str(p):sha(p) for p in folder.rglob('*') if p.is_file()})
    for name in ['http-plan.json', 'budget.json', 'dependency-review.json',
                 'phase4-host-repair.log', 'phase4-http-lifecycle-hooks.log',
                 'http-client-tests.log', 'legacy-parser-test.log',
                 'build-final.log', 'build-fault.log', 'build-lifecycle-final.log',
                 'http-remaining-plan.json', 'stats-0-offline-audit.json',
                 'stats-parser-repair.log', 'frozen/http-runner-original.py',
                 'frozen/http-runner-repaired.py']:
        evidence[str(BASE/name)] = sha(BASE/name)
    save(BASE/'decision.json', dict(passed=True,
        engineering_decision='ACCEPT_BOUNDED_SERVE_RELIABILITY',
        candidate_sha256=sha(plan['candidate']), parent_sha256=sha(plan['parent']),
        fault_sha256=sha(plan['fault']), http=92, starts=18, reviewed=reviewed,
        comparisons=comparisons, evidence=evidence, frozen_tool_relocation=relocated,
        retained_parser_failure=offline,
        performance_acceptance=False, statistical_noninferiority=False,
        limits=['single paired HTTP observations; timing is descriptive',
                'fault/gate evidence uses separate validation binary',
                'no new 200K, full numerical, SSD capacity or suffix claim; '
                'unchanged dependencies reuse identified prior evidence',
                'all-active batching retained; slow-peer effects bounded, not eliminated']))
    print(json.dumps(dict(passed=True, http=92, starts=18,
                         candidate_sha256=sha(plan['candidate']))))


if __name__ == '__main__':
    main()
