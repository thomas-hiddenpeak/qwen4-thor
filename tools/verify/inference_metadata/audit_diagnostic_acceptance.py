"""Offline audit of bounded request diagnostics acceptance; no GPU requests."""
import argparse
import hashlib
import json
from pathlib import Path
import re


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def audit(stage):
    def read(path):
        return json.loads(path.read_text())

    phase = stage / 'acceptance-v1'
    run = phase / 'run'
    guard = read(phase / 'lease/exit.json')
    if guard['status'] != 'COMPLETED' or guard['child_returncode'] != 0:
        raise ValueError('guard incomplete')
    sealed = read(phase / 'sealed-files.json')
    for name, digest in sealed.items():
        path = (phase / name).resolve()
        if not path.is_relative_to(phase.resolve()) or sha(path) != digest:
            raise ValueError('sealed file mismatch: ' + name)
    state = read(run / 'run.json')
    expected = ['host-required', 'quality-off', 'quality-on',
                'diagnostic-contract']
    if state['status'] != 'PASS_RUNTIME_CONTRACTS' or state['completed'] != expected:
        raise ValueError('incomplete acceptance')
    plan = read(run / 'plan.json')
    for name, digest in plan['binaries'].items():
        if sha(run / 'candidate-build' / name) != digest:
            raise ValueError('candidate mismatch')
    if '53 tests, 53 passed, 0 failed, 0 skipped' not in (run / 'host-required.log').read_text():
        raise ValueError('host tests incomplete')
    diagnostic = read(run / 'diagnostic-contract/result.json')
    if diagnostic['status'] != 'PASS' or diagnostic['handlers'] != 7:
        raise ValueError('diagnostic contract incomplete')
    if read(run / 'diagnostic-contract/exit.json')['server'] != 0:
        raise ValueError('diagnostic service exit failed')
    quality = {}
    for mode in ('off', 'on'):
        folder = run / ('quality-' + mode)
        rows = read(folder / 'results.json')
        end = read(folder / 'exit.json')
        if len(rows) != 3 or not all(row['success'] == 1 and row['exact_match']
                                     and row['length_match'] for row in rows):
            raise ValueError('quality failed: ' + mode)
        if end['server'] != 0 or not end['http_output_checks_passed']:
            raise ValueError('server exit failed')
        events = [dict(item.split('=', 1) for item in line.split()[1:])
                  for line in (folder / 'server.log').read_text().splitlines()
                  if line.startswith('[q4t][request]')]
        ends = [event for event in events if event['event'] == 'end']
        if len(ends) != 3 or not all(event['outcome'] == 'success' and
                                    event['released'] == '1' for event in ends):
            raise ValueError('quality diagnostic terminal missing: ' + mode)
        if {event['id'] for event in ends} != {row['response_id'] for row in rows}:
            raise ValueError('response diagnostic identity mismatch: ' + mode)
        if not any(int(event['scheduler_waits']) > 0 for event in ends):
            raise ValueError('scheduler wait not exercised: ' + mode)
        startup = dict(re.findall(r'\[startup\] (\w+) (\d+) ms',
                                  (folder / 'server.log').read_text()))
        quality[mode] = {'passed': len(rows), 'startup_ms': startup}
    if re.search(r'warning:|error:', (stage / 'build.log').read_text()):
        raise ValueError('build diagnostics need review')
    return dict(status='PASS', sealed_files=len(sealed),
                candidate_sha256=plan['binaries']['q4t'],
                source_base_commit=plan['source_commit'],
                source_manifest_sha256=sha(run / 'sources.json'),
                source_patch_sha256=sha(run / 'source.patch'),
                quality=quality, diagnostic_handlers=7, host_tests=53,
                seconds=state['finished'] - state['started'],
                performance_acceptance=False, deployed=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.stage.resolve()), indent=2))
