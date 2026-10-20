"""Offline identity and bounded contract audit. No GPU calls."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def read(p):
    return json.loads(p.read_text())


def audit(stage):
    counts = {}
    for name in ('payload-v1', 'payload-v2', 'payload-v3', 'quality-v1', 'quality-v2'):
        phase = stage/name
        guard = read(phase/'lease/exit.json')
        assert guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0
        files = read(phase/'sealed-files.json')
        for path, expected in files.items():
            target = (phase/path).resolve()
            assert target.is_relative_to(phase.resolve()) and sha(target) == expected
        counts[name] = len(files)
    for name in ('payload-v3',):
        run = stage/name/'run'
        assert read(run/'result.json')['status'] == 'PASS'
        sources = read(run/'sources.json')
        for path in ('src/quant/moe_residency.cpp', 'include/q4t/quant/moe_residency.h'):
            assert sha(ROOT/path) == sources[path]
    first = stage/'payload-v1/run'
    assert '53 tests, 53 passed, 0 failed, 0 skipped' in (first/'host.log').read_text()
    assert '5 tests, 5 passed, 0 failed, 0 skipped' in (first/'residency.log').read_text()
    fault = (stage/'payload-v3/run/payload-faults.log').read_text()
    assert fault.count(': PASS') == 8
    assert '2 tests, 2 passed, 0 failed, 0 skipped' in fault
    run = stage/'quality-v1/run'
    state = read(run/'run.json')
    assert state['status'] == 'PASS_RUNTIME_CONTRACTS'
    assert state['completed'] == ['quality-off','quality-on','quality-offload']
    for mode in ('off','on','offload'):
        folder = run/('quality-'+mode)
        rows = read(folder/'results.json')
        end = read(folder/'exit.json')
        assert len(rows) == 3 and all(r['success'] == 1 and r['exact_match'] and
                                     r['length_match'] for r in rows)
        assert end['server'] == 0 and end['http_output_checks_passed']
    # The only runtime change after v1 moves four host scale reads before
    # releasing their source claim. Ordinary full-resident execution bypasses
    # this entire class; retain its two quality groups rather than resampling.
    current = stage/'quality-v2/run'
    now = read(current/'sources.json')
    before = read(run/'sources.json')
    changed = {p for p in now if p.startswith(('src/','include/')) and
               now[p] != before[p]}
    assert changed == {'src/quant/moe_residency.cpp'}
    for path, expected in now.items():
        assert sha(ROOT/path) == expected
    assert read(current/'run.json')['status'] == 'PASS_RUNTIME_CONTRACTS'
    assert read(current/'run.json')['completed'] == ['quality-offload']
    folder = current/'quality-offload'
    rows, end = read(folder/'results.json'), read(folder/'exit.json')
    assert len(rows) == 3 and all(r['success'] == 1 and r['exact_match'] and
                                 r['length_match'] for r in rows)
    assert end['server'] == 0 and end['http_output_checks_passed']
    assert '5 tests, 5 passed, 0 failed, 0 skipped' in (
        stage/'payload-v3/run/residency.log').read_text()
    for name in ('build-v3.log','build-v4.log','build-v5.log'):
        log = (stage/name).read_text()
        assert 'warning:' not in log and 'error:' not in log
    assert 'Ran 62 tests' in (stage/'tool-contracts-v2-affected.log').read_text()
    assert '\nOK\n' in (stage/'tool-contracts-v2-affected.log').read_text()
    assert 'Ran 27 tests' in (stage/'guard-contracts.log').read_text()
    assert '\nOK\n' in (stage/'guard-contracts.log').read_text()
    run = current
    plan = read(run/'plan.json')
    binary = run/'candidate-build/q4t'
    assert sha(binary) == plan['binaries']['q4t']
    return dict(status='PASS', candidate_sha256=sha(binary), sealed_files=counts,
                host_tests=53, residency_tests=5, payload_fault_cases=9,
                http_quality_requests=12, retained_full_resident_requests=6,
                performance_acceptance=False,
                source_manifest_sha256=sha(run/'sources.json'))


if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--stage',type=Path,required=True)
    args=ap.parse_args()
    print(json.dumps(audit(args.stage.resolve()),indent=2))
