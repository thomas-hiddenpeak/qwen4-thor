"""Freeze only the unfinished R03 business after the discovery-driver failure."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_progress import acceptance as a
from tools.serve_service import package as p


def main():
    p.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
        'commit reviewed continuation driver first')
    base = ROOT / '.q4t-work/serve-progress-20261109'
    prior = base / 'business-01'
    result = p.read(prior / 'execution/result.json')
    p.require(result['status'] == 'FAILED' and result['cleanup_failure'] is None and
        'candidate discovery unavailable' in result['failure'] and
        result['budget']['counts']['starts'] == result['budget']['counts']['chats'] == 1,
        'continuation requires the preserved discovery failure')
    p.require(p.read(base / 'discovery-cpu-01/result.json')['exit_code'] == 0,
        'targeted discovery and budget regression required')
    plan = p.read(prior / 'plan.json')
    changed = [n for n, h in plan['sources'].items() if p.sha(ROOT / n) != h]
    p.require(changed == ['tools/serve_progress/acceptance.py'],
        'only the explicit validation driver may change')
    p.require(p.read(prior / 'lease/exit.json')['status'] == 'COMMAND_FAILED',
        'previous strict lease did not finish cleanly')
    out = base / 'business-02'
    out.mkdir(mode=0o700, exist_ok=False)
    sources = set(plan['sources']) | set(subprocess.check_output(
        ['git', 'ls-files', 'tools/serve_progress'], text=True).splitlines())
    files = [prior / n for n in ('plan.json', 'execution/result.json',
        'execution/counts.json', 'execution/stop.json', 'execution/stopped-ledger.json',
        'lease/exit.json', 'lease/nvml-queries.jsonl')]
    plan.update(scenario='native_progress_completion', output=str(out),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        sources={n: p.sha(ROOT / n) for n in sorted(sources)},
        limits=a.COMPLETION_LIMITS, prior_counts=result['budget']['counts'],
        prior_seconds=result['seconds'], prior_result=str(prior / 'execution/result.json'),
        prior_evidence=[dict(path=str(n), sha256=p.sha(n)) for n in files])
    p.save(out / 'plan.json', plan)
    print(json.dumps(dict(plan=str(out / 'plan.json'), limits=plan['limits'],
        preserved_consumption=plan['prior_counts'], prior_seconds=plan['prior_seconds'],
        runtime_unchanged=True)))


if __name__ == '__main__':
    main()
