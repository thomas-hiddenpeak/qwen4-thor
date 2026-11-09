"""Freeze the targeted completion after N04 discovery-schema failure.

Runtime bytes and the pending package are reused. Corrected public schemas
must be installed in the eventual accepted release, not retrofitted into the
failed candidate or labeled previously observed.
"""
import copy
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_access import acceptance as a
from tools.serve_access.resume import sha

BASE = ROOT/'.q4t-work/serve-access-20261109'


def main():
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT):
        raise ValueError('commit reviewed config/driver before continuation')
    output = BASE/'business-03'
    if output.exists():raise ValueError('continuation exists')
    old = json.loads((BASE/'business-02/plan.json').read_bytes())
    result = json.loads((BASE/'business-02/execution/result.json').read_bytes())
    stop = json.loads((BASE/'business-02/execution/stop.json').read_bytes())
    cpu = json.loads((BASE/'config-cpu-01/result.json').read_bytes())
    driver_cpu = json.loads((BASE/'config-driver-cpu-01/result.json').read_bytes())
    if not (stop['cleanup_confirmed'] and stop['stop_durable'] and stop['state'] == 'STOPPED' and
            cpu['exit_code'] == 0 and cpu['count'] == 4 and
            driver_cpu['exit_code'] == 0 and driver_cpu['count'] == 2):
        raise ValueError('cleanup or config CPU incomplete')
    # Both failed attempts stay charged; this adds only the one consumed
    # model/self-check and does not extend the original total time.
    seconds = sum(json.loads((BASE/f'business-launch-{i:02d}/result.json').read_bytes())['seconds']
                  for i in (1,2))
    plan = copy.deepcopy(old)
    plan.update(output=str(output), limits=a.CONFIG_COMPLETION_LIMITS,
        prior_execution=str(BASE/'business-02/execution/result.json'),
        prior_guard=str(BASE/'business-02/lease/exit.json'),
        prior_counts=result['budget']['counts'],prior_seconds=seconds,
        prior_failure_category='config_schema',
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip())
    allowed = {'tools/serve_access/acceptance.py','tools/serve_access/test_acceptance.py',
               'docs/serve-config-v1.schema.json'}
    changes = []
    for name, digest in old['sources'].items():
        current = sha(ROOT/name)
        if current != digest:
            if name not in allowed:raise ValueError('unreviewed runtime input changed: '+name)
            changes.append(dict(path=name,before=digest,after=current))
        plan['sources'][name] = current
    for name in ('tools/serve_access/complete.py','tools/serve_access/test_config.py',
                 'docs/serve-openapi-bundle-v1.json','docs/serve-input-semantics-v1.json'):
        plan['sources'][name] = sha(ROOT/name)
    plan['source_changes'] = changes
    paths = [BASE/'business-02/plan.json',BASE/'business-02/execution/result.json',
        BASE/'business-02/execution/counts.json',BASE/'business-02/execution/stop.json',
        BASE/'business-02/recovered-configuration.json',*sorted((BASE/'business-02/lease').iterdir()),
        *sorted((BASE/'business-launch-02').iterdir()),BASE/'config-cpu-01/result.json',
        BASE/'config-driver-cpu-01/result.json']
    plan['prior_evidence'] += [dict(path=str(p),sha256=sha(p)) for p in paths if p.is_file()]
    a.verify(plan)
    output.mkdir(mode=0o700);a.save(output/'plan.json',plan)
    print(json.dumps(dict(plan=str(output/'plan.json'),prior_counts=plan['prior_counts'],
        remaining_seconds=plan['limits']['seconds']-seconds,limits=plan['limits'],
        pending_bundle_unchanged=True,accepted_release_requires_current_schemas=True)))


if __name__ == '__main__':main()
