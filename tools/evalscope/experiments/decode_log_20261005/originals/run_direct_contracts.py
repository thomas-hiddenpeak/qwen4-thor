"""One affected host/numerical pass, admitted only after first HTTP quality."""
import argparse
import re
import sys
import time

from phase_common import (R, W, environment, export_identity, frozen,
                          prerequisite, read, run, save, sha)

parser = argparse.ArgumentParser()
parser.add_argument('kind', choices=['host', 'numerical'])
parser.add_argument('--plan-sha256', required=True)
args = parser.parse_args()
plan = frozen(args.plan_sha256)
prerequisite('quality', plan)
if args.kind == 'numerical':
    prerequisite('host', plan)
directory = R / (args.kind + '-01')
directory.mkdir()
started = time.time()
records = []
source_identity = export_identity(plan)
test_binary_before = None
env = environment()
temporary = directory / 'tmp'
temporary.mkdir()
env['TMPDIR'] = str(temporary)


def execute(label, command, cwd=W, timeout=600):
    record = run(command, directory, label, cwd=cwd, env=env, timeout=timeout)
    records.append(record)
    assert (record['returncode'] == 0 and record['failure'] is None and
            record['cleanup_complete']), label
    return (directory / (label + '.log')).read_text()


passed = False
failure = None
groups = []
try:
    if args.kind == 'host':
        for name in plan['host_python_groups']:
            log = execute(name, ['python3', '-B', '-m', 'unittest', name, '-v'],
                          W / 'tools/evalscope')
            count = re.search(r'^Ran (\d+) tests? in ', log, re.M)
            assert count and int(count[1]) > 0
            assert int(count[1]) == plan['host_python_expected_counts'][name]
            assert re.search(r'^OK$', log, re.M)
            groups.append({'name': name, 'passed': int(count[1])})
        contract = plan['host_cpp_contract']
        source_relative = contract['source_relative']
        build_relative = contract['build_directory']
        target = contract['target']
        expected_tests = contract['expected_tests']
        assert type(expected_tests) is int and expected_tests > 0
        assert source_relative == 'tests/request_policy_host'
        assert build_relative == 'request-host-build'
        assert target == 'q4t_request_policy_contracts'
        b = R / build_relative
        log = execute('host-configure', ['cmake', '-S',
            str(R / 'candidate-source' / source_relative), '-B', str(b),
            '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CXX_COMPILER=/usr/bin/g++-14'])
        log += execute('host-build', ['cmake', '--build', str(b), '--parallel', '4'])
        assert not re.search(r'warning\s*:|warning\s*#|cmake\s+warning', log, re.I)
        log = execute('request-host-tests', [str(b / target)])
        expected_summary = (f'{expected_tests} tests, {expected_tests} passed, '
                            '0 failed, 0 skipped')
        assert expected_summary in log.splitlines()
        groups.append({'name': 'request_policy_host', 'passed': expected_tests,
                       'frozen_contract': contract})
    else:
        b = R / 'candidate-build'
        log = execute('numerical-build', ['cmake', '--build', str(b),
                                         '--target', 'q4t_tests', '--parallel', '8'])
        assert not re.search(r'warning\s*:|warning\s*#|cmake\s+warning', log, re.I)
        assert sha(b / 'q4t') == plan['runtime_binary_sha256']
        required = R / 'required-numerical-tests.txt'
        assert required.read_text().splitlines() == plan['required_numerical_tests']
        source_identity = export_identity(plan)
        test_binary_before = sha(b / 'q4t_tests')
        save(directory / 'numerical-identity-before.json', {
            'test_binary_sha256': test_binary_before,
            'runtime_binary_sha256': plan['runtime_binary_sha256'],
            'required_list_sha256': sha(required),
            'required_tests': plan['required_numerical_tests'],
            'build_source': source_identity, 'recorded_t': time.time(),
            'plan_sha256': args.plan_sha256})
        assert plan['required_numerical_tests'] == read(R / 'plan.json')[
            'required_numerical_tests']
        assert plan['numerical_environment'][
            'Q4T_MOE_DECODE_PARTITION_LOG_QUIET'] == '1'
        env.update(plan['numerical_environment'])
        log = execute('numerical-tests', [str(b / 'q4t_tests'), '--required-list',
                                        str(required)], timeout=1800)
        assert sha(b / 'q4t_tests') == test_binary_before
        assert sha(b / 'q4t') == plan['runtime_binary_sha256']
        outcomes = re.findall(r'^\[(PASS|FAIL|SKIP)\] (\w+)(?::.*)?$', log, re.M)
        assert outcomes == [('PASS', n) for n in plan['required_numerical_tests']]
        expected_tests = len(plan['required_numerical_tests'])
        expected_summary = (f'{expected_tests} tests, {expected_tests} passed, '
                            '0 failed, 0 skipped')
        assert expected_summary in log.splitlines()
        request_cases = [line for line in log.splitlines()
                         if line.startswith('  request_policy layer=')]
        expected_cases = plan['numerical_request_policy_cases']
        assert type(expected_cases) is int and expected_cases > 0
        assert len(request_cases) == expected_cases
        assert all('BF16_BIT_EXACT=true' in line for line in request_cases)
        phase_lines = [line.strip() for line in log.splitlines()
                       if line.startswith('  partition_numerical log_phase=')]
        assert phase_lines == plan['numerical_log_phase_records']
        assert log.count('[q4t][residency] decode_partition_log_quiet=1 '
                         'scope=explicit_single_decode\n') == 1
        groups.append({'name': 'real_weight_numerical', 'passed': expected_tests,
                       'request_policy_cases': request_cases,
                       'explicit_log_phase_records': phase_lines,
                       'test_binary_sha256': sha(b / 'q4t_tests')})
    frozen(args.plan_sha256)
    passed = True
except BaseException as error:
    failure = type(error).__name__ + ': ' + str(error)
    raise
finally:
    report = {'schema': 1, 'passed': passed, 'failure': failure,
        'started_t': started, 'ended_t': time.time(), 'groups': groups,
        'records': records, 'runtime_binary_sha256': plan['runtime_binary_sha256'],
        'runtime_source_commit': plan['runtime_source_commit'],
        'log_sha256': {str(p): sha(p) for p in sorted(directory.glob('*.log'))},
        'build_source': source_identity,
        'test_binary_sha256_before': test_binary_before,
        'test_binary_sha256_after': (sha(R / 'candidate-build/q4t_tests')
            if test_binary_before is not None else None),
        'plan_sha256': args.plan_sha256, 'performance_acceptance': False}
    save(R / (args.kind + '-first-attempt.json'), report)
    if passed:
        save(R / (args.kind + '-decision.json'), report)
    print(report, flush=True)
