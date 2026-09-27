"""Verify test-runner exit/selection/skip contracts without a GPU or model."""
import argparse
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / d) for d in ['build', '.q4t-work'])
    out.mkdir(parents=True, exist_ok=False)
    build = ['g++-14', '-std=c++23', '-Wall', '-Wextra', '-Iinclude',
             'tests/test_main.cpp']
    with (out / 'build.log').open('w') as log:
        for name, extra in [('runner', ['tools/verify/test_gate/cases.cpp']), ('empty', [])]:
            subprocess.run(build + extra + ['-o', str(out / name)], cwd=ROOT,
                           stdout=log, stderr=subprocess.STDOUT, check=True)
    assert 'warning:' not in (out / 'build.log').read_text()
    manifests = {'valid': 'ok\n', 'missing': 'ok\nnot_registered\n',
                 'duplicate': 'ok\nok\n', 'skip': 'skip_cleanup\ncleanup_observed\n',
                 'empty': '# no tests\n'}
    for name, body in manifests.items():
        (out / (name + '.txt')).write_text(body)
    cases = [('ok', ['ok'], 0), ('fail', ['bad'], 1), ('skip', ['skip_cleanup'], 1),
             ('dev-skip', ['skip_cleanup', '--allow-skips'], 0),
             ('zero-match', ['absent_filter'], 2), ('exception', ['exception'], 1),
             ('many-failures', ['many_'], 1), ('bad-flag', ['--unknown'], 2),
             ('missing-file', ['--required-list', str(out / 'absent.txt')], 2)]
    for name, code in [('valid', 0), ('missing', 2), ('duplicate', 2), ('skip', 1), ('empty', 2)]:
        cases.append(('required-' + name, ['--required-list', str(out / (name + '.txt'))], code))
    cases.append(('required-no-skip-override', ['--required-list', str(out / 'valid.txt'), '--allow-skips'], 2))
    results = []
    for name, argv, code in cases:
        r = subprocess.run([str(out / 'runner')] + argv, text=True, capture_output=True)
        (out / (name + '.log')).write_text(r.stdout + r.stderr)
        assert r.returncode == code, (name, r.returncode, code)
        if code == 2:
            assert '[PASS]' not in r.stdout and '[FAIL]' not in r.stdout
        if name in ['skip', 'dev-skip']:
            assert '[SKIP] skip_cleanup' in r.stdout and '[PASS] skip_cleanup' not in r.stdout
        if name == 'required-skip':
            assert '[PASS] cleanup_observed' in r.stdout
        results.append({'case': name, 'exit': code})
    r = subprocess.run([str(out / 'empty')], text=True, capture_output=True)
    assert r.returncode == 2
    results.append({'case': 'empty-registry', 'exit': r.returncode})
    (out / 'summary.json').write_text(json.dumps(results, indent=2))
    print(f'{len(results)} runner contract cases passed')


if __name__ == '__main__':
    main()
