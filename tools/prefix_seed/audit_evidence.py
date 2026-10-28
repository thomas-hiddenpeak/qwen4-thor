"""CPU-only final budget, identity and monitor audit, retaining failed A."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/prefix-seed-20261028'


def read(path):
    return json.loads(path.read_text())


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def resolve(path, digest):
    source = Path(path)
    if source.exists() and sha(source) == digest:
        return source
    original = BASE / 'fixture-revision/original-run_stage.py'
    if (source == ROOT / 'tools/prefix_seed/run_stage.py'
            and sha(original) == digest):
        return original
    raise ValueError('Unresolved historical source: ' + path)


def main():
    identity = read(BASE / 'identity.json')
    phases, failures = [], []
    generated = rejected = starts = fixtures = cancelled = samples = 0
    names = ['phase-a', 'phase-a-remaining', 'phase-b', 'phase-c', 'phase-d',
             'deployment']
    for name in names:
        directory = BASE / name
        if not (directory / 'run/state.json').exists():
            continue
        plan = read(directory / 'plan.json')
        state = read(directory / 'run/state.json')
        if state['status'] == 'RUNNING':
            raise ValueError('Cannot finalize a live phase')
        post = read(directory / 'postflight.json')
        guard = read(directory / 'lease/exit.json')
        guard_finished = datetime.fromisoformat(guard['finished']['utc']).timestamp()
        summary = read(directory / 'run/telemetry-summary.json')
        original = name == 'phase-a'
        if (state.get('monitor_failures') or post.get('monitor_failures')
                or post['status'] == 'ENV_ABORT' or post['final_monitor_errors']
                or post['cleanup_errors'] or summary.get('sampler_failure')
                or summary.get('failures_at_exit')
                or summary.get('exit_pending', {}).get('active')
                or summary['sample_attempts'] != summary['flushed_samples']
                or summary['unflushed_attempts']):
            failures.append(name + ': environment/monitor')
        if original:
            if (state['status'] != 'STOPPED' or not
                    read(BASE / 'fixture-revision/full-generation-review.json')['passed']):
                failures.append(name + ': retained failure changed')
        elif (state['status'] != 'MEASURED_REQUIRES_REVIEW'
                or guard['status'] != 'COMPLETED'
                or not post['final_monitor_barrier']):
            failures.append(name + ': incomplete closure')
        for expected, actual in [
                (post['state_sha256'], sha(directory / 'run/state.json')),
                (post['final_summary_sha256'],
                 sha(directory / 'run/telemetry-summary.json')),
                (state['plan_sha256'], sha(directory / 'plan.json'))]:
            if expected != actual:
                failures.append(name + ': seal identity')
        if max(state['finished'], guard_finished) > identity['deadline']:
            failures.append(name + ': deadline exceeded')
        bindings = {path: str(resolve(path, digest))
                    for path, digest in plan['files'].items()}
        for path, expected in plan['model_file_metadata'].items():
            st = Path(path).stat()
            actual = dict(size=st.st_size, mtime_ns=st.st_mtime_ns,
                          device=st.st_dev, inode=st.st_ino)
            if actual != expected:
                failures.append(name + ': model identity ' + path)
        phase_generated = phase_rejected = phase_starts = phase_cancelled = 0
        for log in (directory / 'run').glob('*/server.log'):
            raw = log.read_text()
            phase_starts += 1
            admitted = re.findall(r'instance=(\d+) .* event=admitted ', raw)
            ends = [dict(re.findall(r'(\w+)=(\S+)', line))
                    for line in raw.splitlines()
                    if line.startswith('[q4t][request] ') and ' event=end ' in line]
            generated_ends = [r for r in ends if r['seq'] != '-1']
            negative_ends = [r for r in ends if r['seq'] == '-1']
            if (set(admitted) != {r['instance'] for r in generated_ends}
                    or len(admitted) != len(generated_ends)
                    or len(set(admitted)) != len(admitted)):
                failures.append(name + ': admitted/end ledger')
            if any(r['outcome'] not in ['success', 'cancelled']
                   for r in generated_ends):
                failures.append(name + ': generation failure')
            if any(r['outcome'] != 'error' for r in negative_ends):
                failures.append(name + ': invalid-request outcome')
            phase_generated += len(admitted)
            phase_rejected += len(negative_ends)
            phase_cancelled += sum(r['outcome'] == 'cancelled'
                                   for r in generated_ends)
            if not original and not read(log.parent / 'exit.json')['http_output_checks_passed']:
                failures.append(name + ': HTTP output audit')
            for line in raw.splitlines():
                if line.startswith('[prefix-cache] request=') and 'error=none' not in line:
                    failures.append(name + ': seed save error ' + line)
        direct = sum(s['kind'] == 'contract' and
                     (directory / 'run' / (s['name'] + '-driver.log')).exists()
                     for s in plan['steps'])
        generated += phase_generated
        rejected += phase_rejected
        starts += phase_starts
        fixtures += direct
        cancelled += phase_cancelled
        samples += summary['flushed_samples']
        phases.append(dict(name=name, status=state['status'],
            started=state['started'], finished=state['finished'],
            guard_finished=guard_finished,
            generated_http=phase_generated, rejected_http=phase_rejected,
            expected_cancellations=phase_cancelled, starts=phase_starts,
            direct_gpu_fixtures=direct, elapsed_seconds=state['elapsed_seconds'],
            source_bindings=bindings, plan_sha256=sha(directory / 'plan.json'),
            sampler_flushed=summary['flushed_samples'],
            final_barrier=bool(post['final_monitor_barrier']),
            limitation=('Original failed stage retains 3 missing rejection '
                        'bodies and no final post-shutdown monitor barrier.'
                        if original else None)))
    if generated + rejected > 300 or rejected > 12 or starts > 20 or fixtures > 2:
        failures.append('Total resource budget exceeded')
    result = dict(passed=not failures, failures=failures, phases=phases,
        cumulative_generated_http=generated, cumulative_rejected_http=rejected,
        cumulative_http=generated + rejected, cumulative_starts=starts,
        cumulative_gpu_fixtures=fixtures, expected_cancellations=cancelled,
        sampler_flushed=samples, deadline=identity['deadline'],
        deadline_remaining_seconds=identity['deadline'] - time.time(),
        first_measurement_started=min(p['started'] for p in phases),
        last_measurement_finished=max(p['guard_finished'] for p in phases),
        measurement_wall_seconds=max(p['guard_finished'] for p in phases)
                                 - min(p['started'] for p in phases),
        note='Evidence audit only; does not imply performance acceptance. '
             'Includes health if run. Wall span ends at the outer guard and '
             'includes fixed IO preparation, release and gaps; summed phase '
             'elapsed values exclude IO preparation.',
        statistical_noninferiority='NOT_ESTABLISHED')
    (BASE / 'final-audit.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'phases'}, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
