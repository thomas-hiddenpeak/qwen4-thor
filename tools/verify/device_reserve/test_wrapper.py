#!/usr/bin/env python3
"""Build/run the actual wrapper with CPU fake CUDA, never load libcudart."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cxx', default='g++-14')
    parser.add_argument('--cuda-include', type=Path,
                        default=Path('/usr/local/cuda/include'))
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    repo = source.parents[2]
    output = args.output.resolve()
    if not output.is_relative_to(repo / '.q4t-work'):
        parser.error('--output must be inside this repo .q4t-work')
    output.mkdir(parents=True, exist_ok=False)
    plan_sha = '0' * 64
    (output / 'reserve_plan.h').write_text('''#include <cstddef>
namespace q4t_reserve_plan {
inline constexpr std::size_t kDeviceSizes[] = {16, 32, 48};
inline constexpr std::size_t kCount = 3;
inline constexpr std::size_t kStartupDeviceBytes = 96;
inline constexpr char kPlanSha256[] = "''' + plan_sha + '''";
inline constexpr char kSourceLogSha256[] = "''' + '1' * 64 + '''";
}
''')
    command = [args.cxx, '-std=c++23', '-Wall', '-Wextra', '-Werror',
               '-I', str(output), '-I', str(source),
               '-I', str(args.cuda_include), str(source / 'memory_trace.cpp'),
               str(source / 'test_wrapper.cpp'), '-pthread', '-ldl',
               '-o', str(output / 'test_wrapper')]
    built = subprocess.run(command, capture_output=True, text=True, check=False)
    (output / 'build.stdout').write_text(built.stdout)
    (output / 'build.stderr').write_text(built.stderr)
    (output / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    built.check_returncode()
    trace = load('trace_audit', source.parent / 'vmm_compare' / 'trace_audit.py')
    reservation = load('reservation_audit', source / 'reservation_audit.py')
    cases = {}
    for mode in ('success', 'mismatch', 'partial', 'early_pinned',
                 'allocation_failure'):
        run = subprocess.run([str(output / 'test_wrapper'), mode],
                             capture_output=True, text=True, timeout=10,
                             check=False)
        path = output / (mode + '.log')
        path.write_text(run.stderr)
        (output / (mode + '.stdout')).write_text(run.stdout)
        expected = 0 if mode == 'success' else 125
        if run.returncode != expected:
            raise AssertionError((mode, run.returncode, run.stderr))
        rows = [json.loads(line.split(' ', 1)[1])
                for line in run.stderr.splitlines()
                if line.startswith('[reserve-trace] ')]
        assert [r['seq'] for r in rows] == list(range(len(rows)))
        assert [r['mono'] for r in rows] == sorted(r['mono'] for r in rows)
        summary = [r for r in rows if r['event'] == 'summary']
        assert len(summary) == 1
        assert summary[0]['status'] == ('PASS' if expected == 0 else 'FAILED')
        assert summary[0]['unhanded_remaining'] == 0
        assert summary[0]['handed_live'] == summary[0]['release_unknown'] == 0
        # Existing independent logical audit enforces per-call identity.
        logical_audit = trace.audit(path)
        if mode == 'success':
            reservation.audit_success(run.stderr.splitlines(), plan_sha,
                                      [16, 32, 48])
            assert '[fake-runtime] finalized' in run.stderr
            assert run.stderr.index('"event":"summary"') < run.stderr.index(
                '[fake-runtime] finalized')
        cases[mode] = {'returncode': run.returncode, 'summary': summary[0],
                       'logical_audit': logical_audit}
    result = {'status': 'PASS', 'host_wrapper_contracts': len(cases),
              'plan_sha256': plan_sha, 'sizes': [16, 32, 48], 'cases': cases}
    (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'status': 'PASS', 'host_wrapper_contracts': len(cases),
                      'success_log': str(output / 'success.log')}))


if __name__ == '__main__':
    main()
