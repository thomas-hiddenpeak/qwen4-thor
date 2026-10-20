"""Offline completeness and identity audit; never starts CUDA or HTTP."""
import argparse
import hashlib
import json
from pathlib import Path
import re


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def audit(stage, attempt="v2"):
    run = stage / ('load-acceptance-' + attempt)
    state = json.loads((run / 'run.json').read_text())
    if state['status'] != 'PASS_RUNTIME_CONTRACTS' or state['completed'] != [
            'upload', 'moe', 'quality-off', 'quality-on']:
        raise ValueError('stage incomplete')
    guard = json.loads((stage / ('load-lease-' + attempt) / 'exit.json').read_text())
    if guard['status'] != 'COMPLETED' or guard['child_returncode'] != 0:
        raise ValueError('GPU guard incomplete')
    manifest = json.loads((run / 'files.json').read_text())
    for name, expected in manifest.items():
        path = (run / name).resolve()
        if not path.is_relative_to(run.resolve()) or sha(path) != expected:
            raise ValueError('manifest mismatch: ' + name)
    plan = json.loads((run / 'plan.json').read_text())
    for name, expected in plan['binaries'].items():
        if sha(run / 'candidate-build' / name) != expected:
            raise ValueError('candidate identity mismatch')
    comparison = json.loads((run / 'moe-parent-comparison.json').read_text())
    if comparison != {'status': 'PASS', 'files': 50}:
        raise ValueError('MoE parent comparison incomplete')
    quality = {}
    for mode in ('off', 'on'):
        folder = run / ('quality-' + mode)
        rows = json.loads((folder / 'results.json').read_text())
        shutdown = json.loads((folder / 'exit.json').read_text())
        if len(rows) != 11 or not all(r['success'] == 1 and r['exact_match']
                                     and r['length_match'] for r in rows):
            raise ValueError('quality failed: ' + mode)
        if shutdown['server'] != 0 or not shutdown['http_output_checks_passed']:
            raise ValueError('server shutdown failed: ' + mode)
        startup = dict(re.findall(r'\[startup\] (\w+) (\d+) ms',
                                  (folder / 'server.log').read_text()))
        quality[mode] = dict(passed=len(rows), startup_ms=startup)

    def sass(path):
        result = []
        for section in path.read_text().split('Function : ')[1:]:
            name = section.splitlines()[0].strip()
            lines = [line.strip() for line in section.splitlines()[1:]
                     if re.search(r'/\*[0-9a-f]+\*/', line)]
            if not lines:
                raise ValueError('empty SASS function')
            result.append((name, hashlib.sha256('\n'.join(lines).encode()).hexdigest()))
        return result

    old = sass(stage / 'parent.sass')
    new = sass(stage / 'candidate.sass')
    if len(old) != 119 or old != new:
        raise ValueError('GPU function names/instructions/encodings changed')
    return dict(status='PASS', sealed_files=len(manifest), quality=quality,
                moe_files=50, sass_functions_identical=len(old),
                candidate_sha256=plan['binaries']['q4t'],
                source_commit=plan['source_commit'],
                gpu_seconds=state['finished'] - state['started'],
                performance_acceptance=False, deployed=False,
                scope='loader correctness; no vision semantics or performance claim')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--attempt', choices=('v1', 'v2'), default='v2')
    args = parser.parse_args()
    print(json.dumps(audit(args.stage.resolve(), args.attempt), indent=2))
