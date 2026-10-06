"""Freeze all fixed inputs, commands, tools and build identities before HTTP."""
from pathlib import Path
import subprocess
import time

from recycle_common import R, W, read, save, sha

MAIN = R.parent.parent
MODEL = MAIN.parent / 'llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=W,
                               text=True).strip()
assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=W).strip()
build = read(R / 'build-identity.json')
assert build['build_rc'] == 0 and build['warnings'] == 0 and build['failure'] is None
assert build['source_commit'] == head
protocol = read(R / 'protocol-draft.json')
assert protocol['service_count'] == 17 and protocol['http_count'] == 131
assert len(protocol['groups']) == 17
hot = MAIN / '.q4t-work/moe-residency-20260930/hot-lists/hot-final-12288.json'
quality_inputs = MAIN / '.q4t-work/moe-trace-runtime-20260928/quality-off/inputs'
history_inputs = MAIN / '.q4t-work/offload-request-policy-20261004/history-fixtures'
matrix_inputs = MAIN / '.q4t-work/moe-residency-20260930/e2e-fixtures-v2'
quality_reference = MAIN / '.q4t-work/offload-partition-runtime-20261003/quality-on/http/results.json'
perf_reference = MAIN / '.q4t-work/offload-partition-runtime-20261003/matrix-off/http/results.json'
binary = R / 'candidate-build/q4t'
assert sha(binary) == build['binary_sha256']
files = {}


def bind(path):
    path = Path(path).resolve()
    assert path.is_file(), path
    files[str(path)] = sha(path)


for p in [hot, MODEL / 'config.json', MODEL / 'model.safetensors.index.json',
          quality_inputs / 'manifest.json', quality_inputs / 'requests.jsonl',
          quality_reference, perf_reference]:
    bind(p)
for length in set(protocol['history']['sequence']):
    bind(history_inputs / f'context-{length}/requests.jsonl')
for length in protocol['matrix']['lengths']:
    bind(matrix_inputs / f'context-{length}/requests.jsonl')

commands = {}
for group in protocol['groups']:
    ident, kind = group['id'], group['kind']
    command = ['/usr/bin/python3', '-B', str(W / 'tools/evalscope/run_budget_experiment.py'),
        '--binary', str(binary), '--model-dir', str(MODEL), '--hot-list', str(hot),
        '--policy-axis', 'mirror-recycle', '--mirror-gpu-recycle', str(group['arm']),
        '--chunk-order', '0', '--partition', '0', '--request-partition', '0',
        '--decode-partition-log-quiet', '0', '--port', '8187',
        '--output', str(W / '.q4t-work/evidence' / ident),
        '--monitor-interval', '1', '--gpu-interval', '10']
    if kind == 'quality':
        command += ['--mode', 'quality', '--fixtures', str(quality_inputs),
            '--reference', str(quality_reference), '--runner-timeout-s', '7200']
    else:
        command += ['--mode', 'performance', '--host-cache-max-bytes', str(16 << 30),
            '--clear-model-cache', '--cold-advice-rounds', '2',
            '--runner-timeout-s', '21600']
        if kind == 'history':
            command += ['--fixtures', str(history_inputs), '--request-policy-sequence']
        else:
            length = group['input_tokens']
            command += ['--fixtures', str(matrix_inputs), '--reference', str(perf_reference),
                        '--perf-lengths', str(length), '--perf-repeats', '3']
            if length == 261887:
                command += ['--target-total', '262144']
    path = R / (ident + '-command.json')
    save(path, command)
    bind(path)
    commands[ident] = sha(path)

required = R / 'required-numerical-tests.txt'
with required.open('x') as stream:
    stream.write('moe_mirror_gpu_recycle_numerical_contract\n')
bind(required)
tool_sha = {}
for p in sorted((W / 'tools/evalscope').glob('*.py')):
    bind(p)
    tool_sha[p.name] = sha(p)
for name in ['scope-plan.json', 'protocol-draft.json', 'source-design.json',
             'runtime-static-review.json', 'protocol-static-review.json',
             'numerical-design.json', 'numerical-static-review.json',
             'numerical-static-finding-01.json', 'numerical-lifecycle-static-finding.json',
             'resource-dependencies.json', 'dependency-admission.json',
             'candidate-source-identity.json', 'build-identity.json',
             'configure-01-exit.json', 'build-01-exit.json',
             'entry.json', 'model-entry.json']:
    bind(R / name)
for name in ['recycle_common.py', 'run_stage.py', 'audit_stage.py',
             'run_direct_contracts.py', 'compare_results.py', 'resource_audit.py',
             'build_candidate.py', 'prepare_execution.py']:
    bind(R / name)
bind(R.parent / 'offload-mechanism-20261006/phase_common.py')
resource = read(R / 'resource-dependencies.json')
for p, h in resource['source_sha256'].items():
    assert sha(p) == h, p
    bind(p)
for p in sorted((W / 'tests/mirror_recycle_host').glob('*')):
    if p.is_file():
        bind(p)
for name in ['tests/model_moe_test.cpp', 'tests/test_main.cpp', 'include/q4t/test.h',
             'include/q4t/quant/moe_mirror_recycle.h', 'include/q4t/quant/moe_residency.h']:
    bind(W / name)
for p in [R / 'runtime-static-review.json', R / 'protocol-static-review.json',
          R / 'numerical-static-review.json']:
    review = read(p)
    assert review['passed'] and not review.get('blocking_findings')
    for path, digest in review['source_sha256'].items():
        assert sha(path) == digest, path
plan = dict(schema=1, scope='gpu_covered_mirror_recycling_v1', created_t=time.time(),
    runtime_source_commit=head, runner_source_commit=head,
    runtime_binary_path=str(binary), runtime_binary_sha256=sha(binary),
    scope_sha256=sha(R / 'scope-plan.json'),
    protocol_plan_path=str(R / 'protocol-draft.json'),
    protocol_plan_sha256=sha(R / 'protocol-draft.json'),
    groups=protocol['groups'], group_ids=[g['id'] for g in protocol['groups']],
    service_count=17, http_count=131, command_sha256=commands, tool_sha256=tool_sha,
    quality_reference_path=str(quality_reference), quality_reference_sha256=sha(quality_reference),
    performance_reference_path=str(perf_reference), performance_reference_sha256=sha(perf_reference),
    min_free_disk_bytes=1 << 30, expected_host_contracts=14, expected_protocol_contracts=10,
    required_numerical_tests=['moe_mirror_gpu_recycle_numerical_contract'],
    numerical_environment={'Q4T_MOE_PARTITION': '0', 'Q4T_MOE_CHUNK_ORDER': '0',
                          'Q4T_MOE_STREAMS': '1', 'Q4T_MOE_EVICT_WEIGHT': '0'},
    numerical_required_markers=[
        'mirror_recycle case=single-decode step=0 layer=2 C=16 T=1 active=1 changed=1',
        'mirror_recycle case=single-decode step=1 layer=2 C=16 T=1 active=1 changed=1',
        'mirror_recycle case=single-decode step=2 layer=2 C=16 T=1 active=1 changed=1',
        'mirror_recycle case=unknown-singleton step=0 layer=2 C=16 T=1 active=0 changed=0',
        'mirror_recycle case=prefill-singleton step=0 layer=2 C=16 T=1 active=0 changed=0',
        'BF16_BIT_EXACT=true OFF_ON_FP32_BIT_EXACT=true'],
    frozen_files=files, resource_read_limits=resource['raw_read_limits'],
    new_runtime_execution_admitted=False,
    stage_order=['quality11_HTTP', 'quality_audit', 'host24', 'numerical1',
                 'history_ABBA_84', 'history_comparison', 'matrix6_36',
                 'full_comparison', 'resources_once', 'independent_result',
                 'final_protection_and_delivery'])
save(R / 'execution-plan.json', plan)
print(dict(execution_plan_sha256=sha(R / 'execution-plan.json'),
           groups=len(commands), bindings=len(files), source_commit=head), flush=True)
