"""One bounded host/numerical batch, only after the first quality HTTP."""
import argparse
import re
import time

from recycle_common import (R, W, read, save, sha, run, environment,
                            frozen, admitted, passed)

parser = argparse.ArgumentParser()
parser.add_argument('kind', choices=['host', 'numerical'])
parser.add_argument('--plan-sha256', required=True)
args = parser.parse_args()
plan = frozen(args.plan_sha256)
admitted(plan, args.plan_sha256)
passed('q01-quality-on', plan, args.plan_sha256)
if args.kind == 'numerical':
    passed('host', plan, args.plan_sha256)
directory = R / (args.kind + '-01')
directory.mkdir()
env = environment()
(directory / 'tmp').mkdir()
env['TMPDIR'] = str(directory / 'tmp')
records = []
report = dict(schema=1, passed=False, failure=None, started_t=time.time(),
    kind=args.kind, plan_sha256=args.plan_sha256,
    runtime_source_commit=plan['runtime_source_commit'],
    runtime_binary_sha256=plan['runtime_binary_sha256'],
    source_sha256={}, groups=[])


def execute(label, command, cwd=W, timeout=600):
    receipt = run(command, directory, label, cwd=cwd, env=env, timeout=timeout)
    records.append(receipt)
    assert receipt['returncode'] == 0 and receipt['failure'] is None, label
    assert receipt['cleanup_complete'], label
    log = directory / (label + '.log')
    report['source_sha256'][str(log)] = sha(log)
    report['source_sha256'][str(directory / (label + '-exit.json'))] = sha(
        directory / (label + '-exit.json'))
    return log.read_text()


try:
    if args.kind == 'host':
        b = R / 'host-build'
        log = execute('host-configure', ['cmake', '-S',
            str(R / 'candidate-source/tests/mirror_recycle_host'), '-B', str(b),
            '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CXX_COMPILER=/usr/bin/g++-14'])
        log += execute('host-build', ['cmake', '--build', str(b), '--parallel', '4'])
        assert not re.search(r'warning\s*:|warning\s*#|cmake\s+warning', log, re.I)
        log = execute('host-tests', [str(b / 'q4t_mirror_recycle_contracts')])
        assert '14 tests, 14 passed, 0 failed, 0 skipped' in log.splitlines()
        report['groups'].append(dict(name='mirror_recycle_host', passed=14))
        log = execute('protocol-tests', ['/usr/bin/python3', '-B', '-m',
            'unittest', '-v', 'test_mirror_recycle_protocol'], W / 'tools/evalscope')
        count = re.search(r'^Ran (\d+) tests? in ', log, re.M)
        assert count and int(count[1]) == 10 and re.search(r'^OK$', log, re.M)
        report['groups'].append(dict(name='mirror_recycle_protocol', passed=10))
    else:
        b = R / 'candidate-build'
        log = execute('numerical-build', ['cmake', '--build', str(b),
            '--target', 'q4t_tests', '--parallel', '8'], timeout=1800)
        assert not re.search(r'warning\s*:|warning\s*#|cmake\s+warning', log, re.I)
        assert sha(b / 'q4t') == plan['runtime_binary_sha256']
        required = R / 'required-numerical-tests.txt'
        assert required.read_text().splitlines() == plan['required_numerical_tests']
        test_sha = sha(b / 'q4t_tests')
        save(directory / 'identity-before.json', dict(test_binary_sha256=test_sha,
            runtime_binary_sha256=plan['runtime_binary_sha256'],
            required_tests=plan['required_numerical_tests'],
            environment=plan['numerical_environment'], recorded_t=time.time()))
        report['source_sha256'][str(directory / 'identity-before.json')] = sha(
            directory / 'identity-before.json')
        report['source_sha256'][str(required)] = sha(required)
        report['source_sha256'][str(b / 'q4t_tests')] = test_sha
        env.update(plan['numerical_environment'])
        log = execute('numerical-tests', [str(b / 'q4t_tests'), '--required-list',
            str(required)], timeout=1800)
        assert sha(b / 'q4t_tests') == test_sha
        assert sha(b / 'q4t') == plan['runtime_binary_sha256']
        outcomes = re.findall(r'^\[(PASS|FAIL|SKIP)\] (\w+)(?::.*)?$', log, re.M)
        assert outcomes == [('PASS', n) for n in plan['required_numerical_tests']]
        assert '1 tests, 1 passed, 0 failed, 0 skipped' in log.splitlines()
        for marker in plan['numerical_required_markers']:
            assert marker in log, marker
        report['groups'].append(dict(name='mirror_recycle_real_weight', passed=1,
            test_binary_sha256=test_sha, scope='Fixed layer2/T1/C16/L28/K8/one worker'))
    frozen(args.plan_sha256)
    report['passed'] = True
except BaseException as error:
    report['failure'] = type(error).__name__ + ': ' + str(error)
    raise
finally:
    report.update(ended_t=time.time(), recorded_t=time.time(), records=records,
                  performance_acceptance=False)
    save(R / (args.kind + '-first-attempt.json'), report)
    if report['passed']:
        save(R / (args.kind + '-decision.json'), report)
    print({k: report[k] for k in ('kind', 'passed', 'failure', 'groups')}, flush=True)
