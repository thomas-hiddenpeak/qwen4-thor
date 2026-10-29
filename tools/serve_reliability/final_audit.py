"""Bind the narrow terminal repair to reusable evidence and remaining budget."""
import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/serve-reliability-20261030'
sys.path.insert(0, str(ROOT/'tools/evalscope'))
from serve_reliability import sha, save


def main():
    plan = json.loads((BASE/'repair-plan.json').read_text())
    prior = json.loads((BASE/'decision.json').read_text())
    result = json.loads((BASE/'terminal-repair/http/result.json').read_text())
    guard = json.loads((BASE/'terminal-lease/exit.json').read_text())
    assert result['passed'] and result['http']==4 and result['starts']==3
    assert result['completed'] == [s['name'] for s in plan['steps']]
    assert guard['status']=='COMPLETED' and guard['child_returncode']==0
    assert result['finished'] < plan['deadline_unix']
    for path, digest in (prior['evidence'] | plan['files']).items():
        assert sha(path)==digest, path
    for step in plan['steps']:
        folder = BASE/'terminal-repair/http'/step['name']
        exit_record = json.loads((folder/'exit.json').read_text())
        assert exit_record['passed'] and exit_record['requests']==step['requests']
        raw = (folder/'server.log').read_text()
        final = [json.loads(line.split('] ',1)[1]) for line in raw.splitlines()
                 if line.startswith('[startup-reservation] ') and
                 '"event":"finalized"' in line]
        assert len(final)==1 and final[0]['status']=='PASS'
        assert final[0]['handed_live']==final[0]['unhanded_remaining']==0
    host = (BASE/'terminal-host.log').read_text()
    assert '2 tests, 2 passed, 0 failed, 0 skipped' in host
    for name in ['build-terminal.log','build-terminal-fault.log']:
        raw = (BASE/name).read_text()
        assert 'warning:' not in raw and 'error:' not in raw
    delta = json.loads((BASE/'terminal-repair/dependency-review.json').read_text())
    assert delta['reused_http']==92 and delta['reused_starts']==18
    decision = copy.deepcopy(prior)
    decision.update(candidate_sha256=sha(plan['candidate']),
        fault_sha256=sha(plan['fault']), http=96, starts=21,
        initial_candidate_superseded=prior['candidate_sha256'],
        source_commit=plan['source_commit'], terminal_repair=result,
        terminal_dependency_review=delta, planned_health_http=6,
        planned_health_starts=2, final_planned_http=102, final_planned_starts=23)
    for directory in [BASE/'terminal-repair', BASE/'terminal-lease']:
        decision['evidence'].update({str(p):sha(p) for p in directory.rglob('*') if p.is_file()})
    for name in ['repair-plan.json','decision.json','terminal-host.log',
                 'build-terminal.log','build-terminal-fault.log',
                 'delivery-package-superseded.json']:
        decision['evidence'][str(BASE/name)] = sha(BASE/name)
    assert not (BASE/'final-decision.json').exists()
    save(BASE/'final-decision.json', decision)
    print(json.dumps(dict(passed=True, candidate_sha256=decision['candidate_sha256'],
                         measured_http=96, measured_starts=21)))


if __name__ == '__main__':
    main()
