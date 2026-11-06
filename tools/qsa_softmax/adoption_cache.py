"""Two-head bounded cache lifecycle on the frozen QSA adoption candidate."""
import argparse
import importlib.util
from pathlib import Path
import signal
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location('qsa_adoption',
    Path(__file__).with_name('adoption.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
driver = a.module('qsa_cache_driver', 'tools/mtp_prefill_scoring/driver.py')
ROOT, BASE = a.ROOT, a.BASE / 'cache'
read, save, sha, require = a.read, a.save, a.sha, a.require


def prepare():
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit before freezing')
    require(read(a.BASE / 'contracts/review.json')['passed'], 'direct contracts required')
    h = read(a.PLAN)
    original = a.ORIGINAL / '.q4t-work/mtp-prefill-adoption-20261104/source/.q4t-work/scoring-acceptance/plan.json'
    old = read(original)
    frozen = BASE / 'frozen'
    frozen.mkdir(parents=True)
    require(not (BASE / 'plan.json').exists(), 'fresh plan')
    source = Path(old['development_prompt'])
    require(sha(source) == old['files'][str(source)], 'cache input identity')
    target = frozen / 'development.txt'
    target.write_bytes(source.read_bytes())
    plan = dict(stage='QSA_SOFTMAX_ADOPTION_CACHE', output=str(BASE / 'run'),
        model=h['model'], model_file_metadata=h['model_file_metadata'],
        candidate=h['candidate'], artifact=h['artifact'],
        execution_tools_root=str(a.ORIGINAL), port=8762,
        development_prompt=str(target), short_prompt=old['short_prompt'],
        http_loads=2, http_requests=24, stage_timeout_seconds=840,
        approved_total_wall_seconds=900, direct_contract_timeout_seconds=0,
        resource_sample_max_age_seconds=10,
        telemetry_sources=a.telemetry.discover_sources(), steps=[], files={})
    for index, head in enumerate(('full', 'subset')):
        name = f'{index:02d}-{head}-candidate'
        plan['steps'].append(dict(name=name, kind='lifecycle', head=head,
            binary=h['candidate'], layout='agent', suffix=True,
            http_loads=1, http_requests=12, timeout=420,
            command=[sys.executable, '-B', str(Path(__file__).resolve()),
                     '--plan', str(BASE / 'plan.json'), '--step', name]))
    files = {Path(p) for p in h['files']}
    tracked = subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).splitlines()
    files.update(ROOT / p for p in tracked if p.startswith(('src/', 'include/', 'tools/')))
    files.update([target, original, a.PLAN, a.PLAN.parent / 'review.json',
                  a.BASE / 'contracts/review.json',
                  ROOT / 'docs/QSA_SOFTMAX_CONTRACT_PLAN_2026-11-06.md'])
    plan['files'] = {str(p): sha(p) for p in sorted(files)}
    plan['task_deadline_unix'] = time.time() + 840
    save(BASE / 'plan.json', plan)
    print('PREPARED 2 starts / 24 HTTP / 900 seconds')


def validate(plan, executing=True):
    require((plan['stage'], plan['http_loads'], plan['http_requests'],
             plan['stage_timeout_seconds'], plan['approved_total_wall_seconds']) ==
            ('QSA_SOFTMAX_ADOPTION_CACHE', 2, 24, 840, 900), 'cache frozen budget')
    require([s['head'] for s in plan['steps']] == ['full', 'subset'], 'head coverage')
    for s in plan['steps']:
        require((s['http_loads'], s['http_requests'], s['timeout'], s['kind'],
                 s['layout'], s['suffix'], s['binary']) ==
                (1, 12, 420, 'lifecycle', 'agent', True, plan['candidate']),
                'cache step scope')
    if executing:
        require(time.time() < plan['task_deadline_unix'], 'cache absolute deadline')


def review(plan, lease):
    validate(plan, executing=False)
    a.base.monitored_stage.require_complete_monitor(plan)
    a.legacy.verify_files(plan)
    require(read(lease / 'exit.json')['status'] == 'COMPLETED', 'outer lease')
    output = Path(plan['output'])
    for name, digest in read(output / 'files.json').items():
        require(sha(output / name) == digest, 'sealed cache evidence changed')
    results = {}
    for s in plan['steps']:
        out = output / s['name']
        driver.audit(out, 12)
        result = read(out / 'lifecycle.json')
        require(result['passed'] and result['requests'] == 12 and
                result['rejected_http'] == 0 and result['cancel_unpublished'] and
                result['readonly_zero_writes'] and result['duplicate_no_write'],
                'cache lifecycle contract')
        results[s['head']] = result
    save(BASE / 'review.json', dict(passed=True, results=results, http_requests=24,
        services=2, performance_claim=False))
    print('PASS both-head cache lifecycles')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--plan', type=Path, default=BASE / 'plan.json')
    parser.add_argument('--step')
    parser.add_argument('--review', action='store_true')
    parser.add_argument('--lease', type=Path)
    args = parser.parse_args()
    if args.prepare:
        prepare()
        return
    plan = read(args.plan)
    if args.review:
        require(args.lease is not None, 'completed lease required')
        review(plan, args.lease)
        return
    validate(plan)
    if args.step:
        driver.custom_step(plan, args.step)
    else:
        a.base.monitored_stage.run_stage(a.legacy, a.telemetry, plan, args.plan,
                                       audit_server=driver.audit)


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('adoption cache interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    main()
