"""Freeze completed source/build, prepared inputs, commands and controllers."""
from pathlib import Path
import subprocess
import time

from phase_common import R, W, read, save, sha

build = read(R / 'build-identity.json')
assert build['build_rc'] == build['warnings'] == 0
assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=W).strip()
assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=W,
    text=True).strip() == build['source_commit']
assert sha(build['binary']) == build['binary_sha256']
old = R.parent / 'offload-partition-runtime-20261003'
quality = old / 'quality-on/http/results.json'
performance = old / 'matrix-off/http/results.json'
tools = ['run_budget_experiment.py', 'run_acceptance.py', 'isolated_service.py',
    'monitor_memory.py', 'file_cache.py', 'resource_metrics.py',
    'memory_accounting.py', 'offload_policy.py', 'request_policy_protocol.py']
bound = [R / name for name in ['plan.json', 'candidate-source.tar',
    'candidate-source-identity.json', 'build-identity.json', 'entry.json',
    'model-entry.json', 'required-numerical-tests.txt', 'phase_common.py',
    'run_stage.py', 'run_direct_contracts.py', 'audit_group.py',
    'freeze_execution.py', 'build_candidate.py']]
bound += [W / 'tools/evalscope' / n for n in tools]
bound += [quality, performance, Path(build['binary']).parent / 'CMakeCache.txt',
          Path(build['binary']).parent / 'compile_commands.json']
bound += sorted((R / 'history-fixtures').rglob('*.jsonl'))
quality_fixtures = R.parent / 'moe-trace-runtime-20260928/quality-off/inputs'
bound += [quality_fixtures / 'manifest.json', quality_fixtures / 'requests.jsonl']
performance_fixtures = R.parent / 'moe-residency-20260930/e2e-fixtures-v2'
lengths = [1024, 4096, 8192, 45056, 204800, 261887]
bound += [performance_fixtures / f'context-{n}/requests.jsonl' for n in lengths]
hot = R.parent / 'moe-residency-20260930/hot-lists/hot-final-12288.json'
model = Path('/home/rm01/models/dev/llm/garnermccloud/'
             'Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
bound += [hot, model / 'config.json', model / 'model.safetensors.index.json']
stages = ['quality-on', 'inheritance-off', 'inheritance-on', 'matrix-off', 'matrix-on']
bound += [R / (s + '-command.json') for s in stages]
plan = {'schema': 1, 'frozen_at': time.time(),
    'phase_plan_sha256': sha(R / 'plan.json'),
    'runtime_source_commit': build['source_commit'],
    'runtime_binary_path': build['binary'],
    'runtime_binary_sha256': build['binary_sha256'],
    'build_identity_path': str(R / 'build-identity.json'),
    'source_archive_path': str(R / 'candidate-source.tar'),
    'source_directory': str(R / 'candidate-source'),
    'quality_reference_path': str(quality), 'quality_reference_sha256': sha(quality),
    'performance_reference_path': str(performance),
    'performance_reference_sha256': sha(performance),
    'matrix_lengths': lengths,
    'tool_sha256': {n: sha(W / 'tools/evalscope' / n) for n in tools},
    'command_sha256': {s: sha(R / (s + '-command.json')) for s in stages},
    'host_python_groups': ['test_run_acceptance', 'test_partition_protocol',
        'test_chunk_order_protocol', 'test_offload_matrix_audit',
        'test_request_policy_protocol'],
    'required_numerical_tests': (R / 'required-numerical-tests.txt').read_text().splitlines(),
    'numerical_environment': {'Q4T_MOE_PARTITION': '1',
        'Q4T_MOE_REQUEST_PARTITION': '1', 'Q4T_MOE_CHUNK_ORDER': '0',
        'Q4T_MOE_STREAMS': '1', 'Q4T_MOE_EVICT_WEIGHT': '0',
        'Q4T_MOE_L2_SLOTS': '16', 'Q4T_MOE_MIRROR_K': '8',
        'Q4T_MOE_MAX_OPEN_SHARDS': '200', 'Q4T_MOE_PREAD_MERGE': '1',
        'Q4T_MOE_INLINE_MISS_LIMIT': '1'},
    'frozen_files': {str(p): sha(p) for p in bound},
    'whole_physical_RAM_54GB': 'INDETERMINATE', 'automatic_retry': False,
    'performance_acceptance': False}
save(R / 'execution-plan.json', plan)
print(sha(R / 'execution-plan.json'))
