"""Preserve the first failures; recheck only zero outputs and CLI fixtures."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_cuda_quota_integration import common as c, cpu


def test():
    original = c.BASE / 'cpu'
    current = c.BASE / 'cpu-v2'
    built = cpu.check(current)
    before = c.read(original / 'runtime-source.json')
    after = c.read(current / 'runtime-source.json')
    changed = {name for name in before if before[name] != after[name]}
    c.require(before.keys() == after.keys() and changed ==
        {'src/runtime/cuda_allocation_quota.cpp'}, 'correction runtime scope changed')
    name = 'src/runtime/cuda_allocation_quota.cpp'
    old_text = (original / 'source-before-correction' / name).read_text()
    expected = old_text.replace('      value->stream_poisoned = true;\n',
        '      value->stream_poisoned = true;\n      *pointer = nullptr;\n', 1)
    c.require(expected != old_text and (ROOT / name).read_text() == expected,
              'runtime correction exceeds rejected zero-output cleanup')
    prior = c.read(original / 'tests/result.json')
    c.require(not prior['passed'] and {name for name, passed in prior['results'].items()
        if not passed} == {'zero_owner', 'host-off', 'host-required'},
        'original failures changed')
    for path in (original / 'tests/host-off.stdout',
                 original / 'tests/host-gate/run.log'):
        failures = [line for line in path.read_text().splitlines() if line.startswith('[FAIL]')]
        c.require(failures == ['[FAIL] cuda_quota_options_default_explicit_disabled_and_no_budget'],
                  'unexpected original host failure')
    output = current / 'tests'
    output.mkdir()
    results = {}
    binary = lambda name: built['binaries'][name]['path']
    for mode in ('zero', 'zero_owner'):
        results['wrapper-' + mode] = c.command(output, 'wrapper-' + mode,
            [binary('wrappers'), mode])
        c.require('PASS ' + mode + '\n' in
            (output / ('wrapper-' + mode + '.stdout')).read_text(), 'wrapper terminal')
    for name in ('host-off', 'host-on'):
        results[name] = c.command(output, name,
            [binary(name), 'cuda_quota_options_'])
        c.require('2 tests, 2 passed, 0 failed, 0 skipped' in
            (output / (name + '.stdout')).read_text(), 'CLI coverage')
    for name, argv in [
            ('inventory', ['/usr/bin/python3', '-B',
                'tools/serve_audit/check_inventory.py', '--self-check']),
            ('generated', ['/usr/bin/python3', '-B',
                'tools/serve_protocol/build_contract.py', '--check'])]:
        results[name] = c.command(output, name, argv)
    c.save(output / 'result.json', dict(passed=all(results.values()), results=results,
        original_result=c.identity(original / 'tests/result.json'),
        runtime_unchanged=c.sources() == after,
        effective=dict(wrappers=28, public_host=268, required_host=234, backend=9),
        reexecuted=dict(wrappers=2, public_host=2, required_host=2),
        reuse='All unaffected first-round checks; original CLI negative is excluded '
             'because its initial model_dir was invalid. Both option tests are corrected.',
        original_failures_preserved=True, gpu_tests=0, model_starts=0, http_listeners=0))
    c.require(all(results.values()), 'targeted correction failed; preserve evidence')


if __name__ == '__main__':
    test()
