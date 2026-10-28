"""CPU-only evidence/usage audit; partial failures remain partial failures."""
import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/mtp-prefill-release-20261029'


def read(path):
    return json.loads(path.read_text())


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--with-health', action='store_true')
    parser.add_argument('--partial', action='store_true')
    args = parser.parse_args()
    identity = read(BASE/'identity.json')
    phases, errors = [], []
    generated = starts = samples = cancelled = 0
    names = ['a', 'b', 's', 'c'] + (['h'] if args.with_health else [])
    for name in names:
        directory = BASE/('phase-'+name)
        if not (directory/'run/state.json').exists():
            errors.append(name+': not executed')
            continue
        plan, state = read(directory/'plan.json'), read(directory/'run/state.json')
        if state['status'] == 'RUNNING':
            raise RuntimeError('Do not finalize a live stage')
        post = read(directory/'postflight.json')
        guard = read(directory/'lease/exit.json')
        telemetry = read(directory/'run/telemetry-summary.json')
        finished = datetime.fromisoformat(guard['finished']['utc']).timestamp()
        if (state['status'] != 'MEASURED_REQUIRES_REVIEW' or guard['status'] != 'COMPLETED' or
                post['status'] != state['status'] or not post['final_monitor_barrier']):
            errors.append(name+': incomplete stage/guard/monitor barrier')
        if (state.get('monitor_failures') or post['monitor_failures'] or
                post['cleanup_errors'] or post['final_monitor_errors'] or
                telemetry['sampler_failure'] or telemetry['failures_at_exit'] or
                telemetry['unflushed_attempts'] or telemetry['exit_pending']['active'] or
                telemetry['sample_attempts'] != telemetry['flushed_samples']):
            errors.append(name+': environment/monitor')
        for expected, actual in [(post['state_sha256'], sha(directory/'run/state.json')),
            (post['final_summary_sha256'], sha(directory/'run/telemetry-summary.json')),
            (state['plan_sha256'], sha(directory/'plan.json'))]:
            if expected != actual:
                errors.append(name+': receipt identity')
        if max(state['finished'], finished) > identity['deadline']:
            errors.append(name+': deadline exceeded')
        for path, digest in plan['files'].items():
            if sha(path) != digest:
                errors.append(name+': frozen identity '+path)
        for path, expected in plan['model_file_metadata'].items():
            stat = Path(path).stat()
            if dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                    device=stat.st_dev, inode=stat.st_ino) != expected:
                errors.append(name+': model metadata '+path)
        for path, digest in read(directory/'run/files.json').items():
            if sha(directory/'run'/path) != digest:
                errors.append(name+': raw seal '+path)
        for line in (directory/'lease/events.jsonl').read_text().splitlines():
            record = json.loads(line)
            if record.get('conflicts') or record.get('unreadable_processes'):
                errors.append(name+': guard competition/monitor failure')
                break
        count = loads = cancels = 0
        for log in (directory/'run').glob('*/server.log'):
            raw = log.read_text()
            loads += 1
            admissions = re.findall(r'instance=(\d+) .* event=admitted ', raw)
            ends = [dict(re.findall(r'(\w+)=(\S+)', line)) for line in raw.splitlines()
                    if line.startswith('[q4t][request] ') and ' event=end ' in line]
            ended = [r for r in ends if r['seq'] != '-1']
            if len(admissions) != len(ended) or set(admissions) != {r['instance'] for r in ended}:
                errors.append(name+': admitted/end ledger')
            if any(r['seq'] == '-1' for r in ends):
                errors.append(name+': unplanned rejection')
            if any(r['outcome'] not in ['success', 'cancelled'] or r['released'] != '1' for r in ended):
                errors.append(name+': request outcome/release')
            count += len(admissions)
            cancels += sum(r['outcome'] == 'cancelled' for r in ended)
            exit_path = log.parent/'exit.json'
            if not exit_path.exists() or not read(exit_path)['http_output_checks_passed']:
                errors.append(name+': HTTP output incomplete')
        if count != plan['http_requests'] or loads != plan['http_loads']:
            errors.append(name+': fixed request/start count not completed')
        generated += count
        starts += loads
        cancelled += cancels
        samples += telemetry['flushed_samples']
        phases.append(dict(phase=name, status=state['status'], requests=count, starts=loads,
            expected_cancellations=cancels, started=state['started'], finished=state['finished'],
            guard_finished=finished, elapsed_seconds=state['elapsed_seconds'],
            flushed_samples=telemetry['flushed_samples'], final_barrier=post['final_monitor_barrier'],
            plan_sha256=sha(directory/'plan.json'), bound_files=len(plan['files'])))
    expected = (302, 22) if args.with_health else (296, 20)
    if (generated, starts) != expected:
        errors.append('complete matrix count differs from '+repr(expected))
    if generated > 302 or starts > 22:
        errors.append('total fixed budget exceeded')
    result = dict(passed=not errors, errors=errors, candidate_sha256=identity['candidate_sha256'],
        phases=phases, cumulative_http=generated, cumulative_starts=starts,
        new_gpu_fixtures=0, reused_gpu_fixtures=2, expected_cancellations=cancelled,
        flushed_samples=samples, deadline=identity['deadline'],
        deadline_remaining_seconds=identity['deadline']-time.time(),
        statistical_noninferiority='NOT_ESTABLISHED', performance_decision_separate=True,
        retained_preparation_failures=[read(p) for p in sorted(BASE.glob('preparation-failure*.json'))])
    if phases:
        result['measurement_wall_seconds'] = max(p['guard_finished'] for p in phases)-min(p['started'] for p in phases)
    target = 'final-audit-health.json' if args.with_health else ('partial-audit.json' if args.partial else 'final-audit.json')
    (BASE/target).write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'phases'}, indent=2))
    if errors and not args.partial:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
