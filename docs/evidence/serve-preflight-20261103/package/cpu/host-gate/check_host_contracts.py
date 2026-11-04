"""Run the fixed host prerequisite gate; this is not whole-runner release approval."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / 'tools/release/required_host_tests.txt'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--test-binary', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work'])
    out.mkdir(parents=True, exist_ok=False)
    required = [s for s in MANIFEST.read_text().splitlines() if s and not s.startswith('#')]
    result = {'passed': False, 'scope': 'host prerequisites only',
              'required_tests': required, 'binary': str(args.test_binary.resolve()),
              'binary_sha256': sha(args.test_binary), 'manifest_sha256': sha(MANIFEST),
              'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()}
    (out / 'required_host_tests.txt').write_bytes(MANIFEST.read_bytes())
    (out / 'check_host_contracts.py').write_bytes(Path(__file__).read_bytes())
    try:
        assert required and len(set(required)) == len(required)
        listed = subprocess.run([str(args.test_binary.resolve()), '--list'],
                                capture_output=True, text=True, timeout=180)
        (out / 'list.log').write_text(listed.stdout + listed.stderr)
        registered = listed.stdout.splitlines()
        assert listed.returncode == 0 and len(set(registered)) == len(registered)
        assert set(required).issubset(registered), 'required tests not registered'
        with (out / 'run.log').open('w') as log:
            run = subprocess.run([str(args.test_binary.resolve()), '--required-list',
                                  str(MANIFEST)], stdout=log, stderr=subprocess.STDOUT,
                                 timeout=180)
        result['exit'] = run.returncode
        text = (out / 'run.log').read_text()
        passed = [s[7:] for s in text.splitlines() if s.startswith('[PASS] ')]
        assert run.returncode == 0
        assert len(passed) == len(required) and set(passed) == set(required)
        assert '[SKIP]' not in text and '[FAIL]' not in text
        assert f'{len(required)} tests, {len(required)} passed, 0 failed, 0 skipped' in text
        result['passed'] = True
    except Exception as exc:
        result['failure'] = repr(exc)
        raise
    finally:
        (out / 'result.json').write_text(json.dumps(result, indent=2))
    print(f'{len(required)} required host contracts passed; runtime gates remain separate')


if __name__ == '__main__':
    main()
