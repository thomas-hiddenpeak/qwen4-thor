"""Resume only a cleaned N04 attempt that never started a model.

The prior successful probe and all spent counts/time remain charged to the
original bound. No production bundle or previous evidence is rewritten.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_access import acceptance as a


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prior', type=Path, required=True)
    parser.add_argument('--launch', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT):
        raise ValueError('commit reviewed driver before sealing continuation')
    prior = args.prior.resolve(strict=True)
    output = args.output.resolve()
    if not output.is_relative_to(ROOT/'.q4t-work') or output.exists():
        raise ValueError('new dedicated work directory required')
    old = json.loads((prior/'plan.json').read_bytes())
    if old.get('prior_evidence'):
        raise ValueError('this continuation only handles the first zero-model failure')
    plan = copy.deepcopy(old)
    allowed = {'tools/serve_access/acceptance.py','tools/serve_access/test_acceptance.py'}
    changes = []
    for name, digest in old['sources'].items():
        current = sha(ROOT/name)
        if current != digest:
            if name not in allowed:raise ValueError('production input changed: '+name)
            changes.append(dict(path=name,before=digest,after=current))
        plan['sources'][name] = current
    plan['sources']['tools/serve_access/resume.py'] = sha(__file__)
    execution = prior/'execution'
    result = json.loads((execution/'result.json').read_bytes())
    launch = args.launch.resolve(strict=True)
    elapsed = json.loads((launch/'result.json').read_bytes())
    stop = json.loads((execution/'stop.json').read_bytes())
    if not stop['cleanup_confirmed'] or not stop['stop_durable'] or stop['state'] != 'STOPPED':
        raise ValueError('prior cleanup not confirmed')
    plan.update(output=str(output),
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        prior_execution=str(execution/'result.json'),prior_guard=str(prior/'lease/exit.json'),
        prior_probe=str(execution/'device-probe.json'),prior_counts=result['budget']['counts'],
        prior_seconds=elapsed['seconds'],source_changes=changes)
    paths = [prior/'plan.json',execution/'result.json',execution/'counts.json',
        execution/'stop.json',execution/'device-probe.json',execution/'stopped-ledger.json',
        *sorted((prior/'lease').iterdir()),*sorted(launch.iterdir())]
    plan['prior_evidence'] = [dict(path=str(p),sha256=sha(p)) for p in paths if p.is_file()]
    a.verify(plan)
    if plan['prior_seconds'] >= plan['limits']['seconds']-120:
        raise ValueError('no remaining bounded business time')
    output.mkdir(mode=0o700)
    a.save(output/'plan.json',plan)
    print(json.dumps(dict(plan=str(output/'plan.json'),prior_counts=plan['prior_counts'],
                         remaining_seconds=plan['limits']['seconds']-plan['prior_seconds'])))


if __name__ == '__main__':main()
