"""Close A from original valid generation and the bounded remaining checks."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'.q4t-work/prefix-seed-20261028'
def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    original=read(BASE/'fixture-revision/full-generation-review.json')
    folder=BASE/'phase-a-remaining';plan=read(folder/'plan.json');state=read(folder/'run/state.json')
    post=read(folder/'postflight.json');guard=read(folder/'lease/exit.json')
    assert original['passed'] and state['status']==post['status']=='MEASURED_REQUIRES_REVIEW'
    assert guard['status']=='COMPLETED' and post['final_monitor_barrier']
    assert not post['monitor_failures'] and not post['cleanup_errors'] and not post['final_monitor_errors']
    for name in ['00-full-probes','01-subset-on']:
        out=folder/'run'/name;probes=read(out/'rejected-probes.json')
        assert len(probes)==4 and all(r['status']==400 and r['expected'] in r['response'] for r in probes)
        assert read(out/'session-audit.json')['passed']
    out=folder/'run/01-subset-on'
    assert read(out/'first-http-gate.json')['passed'] and read(out/'lifecycle.json')['passed']
    result=dict(passed=True,phase='a',candidate_sha256=plan['candidate_sha256'],generated_requests=46,
        rejected_http_attempts=11,expected_cancellations=2,services=3,cpu_policy_tests=7,cpu_fixture_tests=2,
        original_generation_reuse=original,subset_lifecycle=read(out/'lifecycle.json'),
        original_stage_status='STOPPED_RETAINED',remaining_stage=str(folder),
        state_sha256=sha(folder/'run/state.json'),postflight_sha256=sha(folder/'postflight.json'),guard_sha256=sha(folder/'lease/exit.json'),
        original_rejected_response_bodies_missing=True,statistics='NOT_A_PERFORMANCE_ACCEPTANCE')
    (BASE/'gate-a.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))


if __name__=='__main__':main()
