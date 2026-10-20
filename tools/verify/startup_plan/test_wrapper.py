"""Run the actual dynamic reservation wrapper against CPU fake CUDA."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def main():
    root = Path(__file__).resolve().parents[3]
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    if not out.is_relative_to(root / '.q4t-work'):
        parser.error('output must be within .q4t-work')
    out.mkdir(parents=True, exist_ok=False)
    sources = [here / 'reservation.cpp', here / 'test_wrapper.cpp',
               Path(__file__).resolve(),
               root / 'tools/verify/device_reserve/owner.h',
               root / 'src/trace/sha256.cpp']
    identities = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sources}
    command = ['g++-14', '-std=c++23', '-Wall', '-Wextra', '-Werror',
               '-I' + str(root / 'include'), '-I/usr/local/cuda/include',
               str(here / 'reservation.cpp'), str(here / 'test_wrapper.cpp'),
               str(root / 'src/trace/sha256.cpp'), '-pthread', '-ldl',
               '-o', str(out / 'test_wrapper')]
    built = subprocess.run(command, capture_output=True, text=True)
    (out / 'build.stdout').write_text(built.stdout)
    (out / 'build.stderr').write_text(built.stderr)
    (out / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    built.check_returncode()
    results = {}
    for mode in ('success', 'partial', 'mismatch', 'early_pinned',
                 'allocation_failure', 'extra_before_ready', 'freed_before_ready',
                 'plan_only', 'retry_output_failure'):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        plan = out / f'{mode}-plan.json'
        env['Q4T_STARTUP_PLAN_OUTPUT'] = str(plan)
        if mode == 'plan_only':
            env['Q4T_STARTUP_PLAN_ONLY'] = '1'
        run = subprocess.run([str(out / 'test_wrapper'), mode], env=env,
                             capture_output=True, text=True, timeout=10)
        (out / f'{mode}.log').write_text(run.stderr)
        (out / f'{mode}.stdout').write_text(run.stdout)
        expected = 0 if mode in ('success', 'plan_only', 'retry_output_failure') else 125
        assert run.returncode == expected, (mode, run.returncode, run.stderr)
        rows = [json.loads(line.split(' ', 1)[1])
                for line in run.stderr.splitlines()
                if line.startswith('[reserve-trace] ')]
        assert [r['seq'] for r in rows] == list(range(len(rows)))
        assert [r['mono'] for r in rows] == sorted(r['mono'] for r in rows)
        if mode == 'early_pinned':
            assert rows == [dict(rows[0], event='fatal',
                                 reason='pinned_before_configuration')]
            assert not plan.exists()
        elif mode == 'plan_only':
            assert not rows and '[memory-trace]' not in run.stderr
        else:
            begins = [r for r in rows if r['event'] == 'alloc' and
                      r['phase'] == 'begin']
            ends = [r for r in rows if r['event'] == 'alloc' and
                    r['phase'] == 'end']
            assert [r['index'] for r in begins] == [2, 1, 3, 0]
            assert [r['index'] for r in ends] == [2, 1, 3, 0]
            assert [r['physical_ordinal'] for r in begins] == list(range(4))
            assert [r['physical_ordinal'] for r in ends] == list(range(4))
            for begin, end in zip(begins, ends):
                for field in ('index', 'physical_ordinal', 'bytes', 'tid'):
                    assert begin[field] == end[field]
                assert end['mono'] >= begin['mono']
            pointers = {r['index']: r['ptr'] for r in ends if r['rc'] == 0}
            handoffs = [r for r in rows if r['event'] == 'handoff']
            assert [r['index'] for r in handoffs] == list(range(len(handoffs)))
            for handoff in handoffs:
                assert handoff['ptr'] == pointers[handoff['index']]
            cleaned = [r['index'] for r in rows if r['event'] == 'free' and
                       r['phase'] == 'end' and r['reason'] != 'application']
            if mode == 'allocation_failure':
                assert ends[-1]['rc'] != 0
                assert cleaned == [3, 1, 2]
            elif mode == 'mismatch':
                assert cleaned == [0, 3, 1, 2]
            elif mode == 'partial':
                assert cleaned == [3, 1, 2]
            else:
                assert not cleaned
            summaries = [r for r in rows if r['event'] == 'summary']
            assert len(summaries) == 1
            summary = summaries[0]
            assert summary['status'] == ('PASS' if mode in ('success', 'retry_output_failure')
                                         else 'FAILED')
            assert summary['unhanded_remaining'] == 0
            assert summary['handed_live'] == summary['release_unknown'] == 0
            digest = hashlib.sha256(plan.read_bytes()).hexdigest()
            assert summary['plan_sha256'] == digest
            if mode in ('success', 'retry_output_failure'):
                assert summary['allocated'] == summary['consumed'] == 4
                assert summary['freed'] == 4
                assert run.stderr.index('"event":"startup_complete"') < \
                    run.stderr.index('serving on port')
                assert run.stderr.index('"event":"summary"') < \
                    run.stderr.index('[fake-runtime] finalized')
        if plan.exists():
            data = json.loads(plan.read_text())
            assert [entry['bytes'] for entry in data['allocations']] == \
                [16, 32, 48, 32]
            assert data['count'] == 4 and data['device_bytes'] == 128
            assert data['physical_order'] == {
                'strategy': 'stable_descending_bytes', 'indices': [2, 1, 3, 0]}
        results[mode] = dict(returncode=run.returncode, events=len(rows))
    assert identities == {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sources}
    (out / 'inputs.json').write_text(json.dumps(identities, indent=2) + '\n')
    (out / 'result.json').write_text(json.dumps(dict(
        status='PASS', cases=results, count=len(results), gpu_execution=False,
        physical_order=[2, 1, 3, 0], logical_sizes=[16, 32, 48, 32]),
        indent=2) + '\n')
    manifest = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(out.iterdir()) if path.is_file()}
    (out / 'sealed-files.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(dict(status='PASS', count=len(results))))


if __name__ == '__main__':
    main()
