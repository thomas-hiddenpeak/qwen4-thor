"""Freeze completed build, immutable inputs, commands, and tool identities."""
import hashlib
import json
from pathlib import Path
import subprocess
import time

R = Path(__file__).resolve().parent
W = R / 'source'
OLD = R.parent / 'offload-request-policy-20261004'


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


phase = read(R / 'plan.json')
build = read(R / 'build-identity.json')
prior = read(OLD / 'execution-plan.json')
assert build['warnings'] == 0 and build['build_rc'] == 0
assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=W,
                               text=True).strip() == build['source_commit']
assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=W)
assert sha(Path(build['binary'])) == build['binary_sha256']
stages = ('quality-c', 'history-a', 'history-b', 'history-c',
          'matrix-a', 'matrix-c')
tools = ['run_budget_experiment.py', 'run_acceptance.py', 'isolated_service.py',
         'monitor_memory.py', 'file_cache.py', 'resource_metrics.py',
         'memory_accounting.py', 'offload_policy.py', 'request_policy_protocol.py']
local = ['plan.json', 'candidate-source.tar', 'candidate-source-identity.json',
         'build-identity.json', 'entry.json', 'model-entry.json',
         'required-numerical-tests.txt', 'phase_common.py', 'run_stage.py',
         'run_direct_contracts.py', 'audit_group.py', 'compare_groups.py',
         'run_pipeline.py', 'audit_resources.py', 'build_candidate.py',
         'freeze_execution.py', 'runtime-static-review.json',
         'root-static-tool-review.json']
frozen = {str(R / name): sha(R / name) for name in local}
for path in (R / 'candidate-build/CMakeCache.txt',
             R / 'candidate-build/compile_commands.json'):
    frozen[str(path)] = sha(path)
tool_sha = {name: sha(W / 'tools/evalscope' / name) for name in tools}
for name, digest in tool_sha.items():
    frozen[str(W / 'tools/evalscope' / name)] = digest
# Reuse exact old input/oracle/hot-list/model-configuration bindings, excluding
# the previous phase's implementation, commands, source export and build.
for path, digest in prior['frozen_files'].items():
    p = Path(path)
    external = not p.is_relative_to(OLD)
    history_fixture = p.is_relative_to(OLD / 'history-fixtures')
    if external or history_fixture:
        assert p.suffix not in ('.safetensors', '.bin')
        assert sha(p) == digest, path
        frozen[path] = digest
commands = {}
for stage in stages:
    p = R / (stage + '-command.json')
    command = read(p)
    arm = phase['arms'][stage[-1].upper()]
    expected = {'--binary': build['binary'],
                '--policy-axis': 'request-partition-log',
                '--partition': str(arm['partition']),
                '--request-partition': str(arm['request_partition']),
                '--decode-partition-log-quiet': str(arm['quiet']),
                '--port': '8183',
                '--output': str(W / '.q4t-work/evidence' / stage)}
    for flag, value in expected.items():
        assert command.count(flag) == 1 and command[command.index(flag) + 1] == value
    assert command[:3] == ['python3', '-B', str(W / 'tools/evalscope' / tools[0])]
    assert not Path(expected['--output']).exists()
    commands[stage] = sha(p)
    frozen[str(p)] = commands[stage]
numerical_env = {**prior['numerical_environment'],
                 'Q4T_MOE_DECODE_PARTITION_LOG_QUIET': '1'}
plan = {
    'schema': 1, 'frozen_at': time.time(),
    'phase_plan_sha256': sha(R / 'plan.json'),
    'runtime_source_commit': build['source_commit'],
    'runtime_binary_path': build['binary'],
    'runtime_binary_sha256': build['binary_sha256'],
    'build_identity_path': str(R / 'build-identity.json'),
    'source_archive_path': str(R / 'candidate-source.tar'),
    'source_directory': str(R / 'candidate-source'),
    'quality_reference_path': prior['quality_reference_path'],
    'quality_reference_sha256': prior['quality_reference_sha256'],
    'performance_reference_path': prior['performance_reference_path'],
    'performance_reference_sha256': prior['performance_reference_sha256'],
    'matrix_lengths': phase['matrix']['lengths'],
    'tool_sha256': tool_sha, 'command_sha256': commands,
    'host_python_groups': prior['host_python_groups'],
    'host_python_expected_counts': {
        'test_run_acceptance': 21, 'test_partition_protocol': 27,
        'test_chunk_order_protocol': 31, 'test_offload_matrix_audit': 36,
        'test_request_policy_protocol': 27},
    'host_cpp_contract': {'source_relative': 'tests/request_policy_host',
        'build_directory': 'request-host-build',
        'target': 'q4t_request_policy_contracts', 'expected_tests': 15},
    'required_numerical_tests': phase['required_numerical_tests'],
    'numerical_request_policy_cases': 8,
    'numerical_log_phase_records': [
        'partition_numerical log_phase=unknown T=1024',
        'partition_numerical log_phase=explicit_single_decode T=1',
        'partition_numerical log_phase=unknown T=4'],
    'numerical_environment': numerical_env,
    'frozen_files': frozen, 'whole_physical_RAM_54GB': 'INDETERMINATE',
    'automatic_retry': False, 'performance_acceptance': False,
    'matrix_follows_valid_history_speed_NO_GO': True,
    'history_failure_permanently_vetoes_GO': True,
    'second_candidate_allowed': False}
with (R / 'execution-plan.json').open('x') as stream:
    json.dump(plan, stream, indent=2)
    stream.write('\n')
print('Execution frozen: ' + sha(R / 'execution-plan.json'), flush=True)
