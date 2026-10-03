"""Explicit experiment-axis contracts; no execution or resource side effects."""
import hashlib
import json
import math
from pathlib import Path
import re

AXES = ('chunk-order', 'partition')
BASE_ENVIRONMENT = {
    'Q4T_MOE_L2_SLOTS': '16', 'Q4T_MOE_MIRROR_K': '8',
    'Q4T_MOE_MAX_OPEN_SHARDS': '200', 'Q4T_MOE_EVICT_WEIGHT': '0',
    'Q4T_MOE_PREAD_MERGE': '1', 'Q4T_MOE_INLINE_MISS_LIMIT': '1',
}
RUN_TOOLS = ('run_budget_experiment.py', 'run_acceptance.py',
             'isolated_service.py', 'monitor_memory.py', 'file_cache.py',
             'resource_metrics.py', 'memory_accounting.py')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def policy_environment(chunk_order, partition=0, policy_axis='chunk-order'):
    require(policy_axis in AXES, 'unknown policy axis')
    require(type(chunk_order) is int and chunk_order in (0, 1),
            'chunk-order must be zero or one')
    require(type(partition) is int and partition in (0, 1),
            'partition must be zero or one')
    if policy_axis == 'partition':
        require(chunk_order == 0, 'partition axis requires chunk-order zero')
        return {**BASE_ENVIRONMENT, 'Q4T_MOE_CHUNK_ORDER': '0',
                'Q4T_MOE_PARTITION': str(partition)}
    require(partition == 0, 'chunk-order axis requires partition zero')
    return {**BASE_ENVIRONMENT, 'Q4T_MOE_CHUNK_ORDER': str(chunk_order)}


def check_policy_protocol(protocol, state, policy_axis):
    require(protocol.get('policy_axis', 'chunk-order') == policy_axis,
            'evidence belongs to another policy axis')
    if policy_axis == 'partition':
        require(protocol.get('partition') == state and
                protocol['chunk_order'] == 0,
                'partition state/order differs from frozen experiment')
        expected = policy_environment(0, state, policy_axis)
    else:
        require(protocol['chunk_order'] == state and
                protocol.get('partition', 0) == 0,
                'chunk-order state/partition differs from frozen experiment')
        expected = policy_environment(state)
    require(protocol['effective_environment'] == expected,
            'effective policy environment differs from frozen experiment')


def read_bound_json(path, expected_sha256, sources):
    path = Path(path).resolve()
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    require(isinstance(expected_sha256, str) and
            re.fullmatch(r'[0-9a-f]{64}', expected_sha256) is not None,
            'explicit frozen SHA256 required')
    require(digest == expected_sha256, 'frozen source SHA mismatch: ' + str(path))
    sources[str(path)] = digest
    return json.loads(raw)


def bound_file(path, digest, sources):
    path = Path(path).resolve()
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    require(actual == digest, 'source SHA mismatch: ' + str(path))
    require(str(path) not in sources or sources[str(path)] == actual,
            'source changed while auditing: ' + str(path))
    sources[str(path)] = actual


def check_partition_plan(plan):
    require(plan['policy_axis'] == 'partition', 'not a partition plan')
    require(type(plan['frozen_at']) in (int, float) and
            math.isfinite(plan['frozen_at']), 'invalid plan freeze time')
    for field, length in (('runtime_binary_sha256', 64),
                          ('runtime_source_commit', 40)):
        require(re.fullmatch('[0-9a-f]{' + str(length) + '}', plan[field]),
                'invalid frozen identity: ' + field)
    require(set(plan['tool_sha256']) == set(RUN_TOOLS + ('offload_policy.py',)),
            'partition plan must bind the full runner tool set')
    require(Path(plan['numerical_evidence_path']).is_absolute(),
            'numerical evidence requires an absolute frozen path')
    required = plan['required_numerical_tests']
    require(isinstance(required, list) and len(required) == 2 and
            all(isinstance(name, str) and name for name in required) and
            len(set(required)) == 2, 'plan must freeze both numerical tests')


def check_numerical(plan, expected_sha256, sources, quality_end, pilot_start):
    report = read_bound_json(plan['numerical_evidence_path'], expected_sha256,
                             sources)
    require(report['schema'] == 1 and
            report['status'] == 'PASS_NUMERICAL_CONTRACTS' and
            report['passed'] is True and
            report['runtime_binary_sha256'] == plan['runtime_binary_sha256'] and
            report['runtime_source_commit'] == plan['runtime_source_commit'],
            'numerical evidence failed or belongs to another runtime')
    require(re.fullmatch(r'[0-9a-f]{64}', report['test_binary_sha256']) and
            report['test_binary_sha256'] != report['runtime_binary_sha256'],
            'missing distinct numerical test binary identity')
    for field in ('test_binary_path', 'test_build_identity_path',
                  'execution_record_path'):
        require(Path(report[field]).is_absolute(),
                'numerical artifact path must be absolute: ' + field)
    bound_file(report['test_binary_path'], report['test_binary_sha256'], sources)
    build = read_bound_json(report['test_build_identity_path'],
                            report['test_build_identity_sha256'], sources)
    execution = read_bound_json(report['execution_record_path'],
                                report['execution_record_sha256'], sources)
    for field in ('runtime_source_commit', 'runtime_binary_sha256',
                  'test_binary_sha256'):
        require(build[field] == execution[field] == report[field],
                'numerical build/execution identity mismatch: ' + field)
    require(type(execution['returncode']) is int and execution['returncode'] == 0,
            'numerical execution failed')
    required = report['required_tests']
    counts = report['results']
    require(required == plan['required_numerical_tests'] and
            all(type(counts[key]) is int for key in ('passed', 'failed', 'skipped'))
            and counts['passed'] == 2 and
            counts['failed'] == counts['skipped'] == 0,
            'numerical tests missing, failed or skipped')
    require(all(type(report[field]) in (int, float) and
                math.isfinite(report[field])
                for field in ('started_t', 'ended_t', 'recorded_t')) and
            report['started_t'] == execution['started_t'] and
            report['ended_t'] == execution['ended_t'] and
            quality_end <= report['started_t'] <= report['ended_t'] <=
            report['recorded_t'] <= pilot_start,
            'quality -> numerical -> pilot ordering not established')
    require(report['source_sha256'] and report['logs'],
            'numerical evidence lacks inspectable source/log identities')
    for path, digest in report['source_sha256'].items():
        bound_file(path, digest, sources)
    for log in report['logs']:
        bound_file(log['path'], log['sha256'], sources)
    return {'path': plan['numerical_evidence_path'], 'sha256': expected_sha256,
            'passed': True, 'scope': 'frozen numerical contracts only'}


def partition_path_evidence(log, state):
    require('chunk_order=0 policy=original' in log,
            'chunk-order zero activation missing')
    policy = 'min_new_csr_v1' if state else 'original'
    require(f'partition={state} policy={policy}' in log,
            'partition activation missing')
    lines = [line for line in log.splitlines()
             if '[q4t][residency][partition]' in line]
    if not state:
        require(not lines, 'baseline unexpectedly entered partition candidate')
        return {'partition': 0, 'activation_observed': True,
                'candidate_forwards': 0, 'runtime_eligible': True,
                'numerical_acceptance': False}
    pattern = re.compile(
        r'\[q4t\]\[residency\]\[partition\] layer=(\d+) T=(\d+) '
        r'policy=min_new_csr_v1 requested=1 applied=([01]) fallback=([01]) '
        r'reason=(\w+) chunks=(\d+) singleton_chunks=(\d+) '
        r'work_used=(\d+) work_budget=(\d+) metadata_bytes=(\d+)')
    applied_layers, reasons = set(), {}
    applied_count = singleton_count = fallback_count = 0
    require(lines, 'partition activation has no forward execution records')
    for line in lines:
        match = pattern.search(line)
        require(match is not None, 'malformed partition forward evidence')
        layer, tokens, applied, fallback = map(int, match.groups()[:4])
        reason = match[5]
        chunks, singleton, work, budget, metadata = map(int, match.groups()[5:])
        require(0 <= layer < 48 and tokens > 0 and
                0 < chunks <= tokens and 0 <= singleton <= chunks and
                work >= 0 and budget >= 0 and metadata >= 0,
                'invalid partition forward dimensions/counters')
        if applied:
            require(2 <= tokens <= 8192 and not fallback and
                    reason == 'candidate' and
                    budget == 32 * tokens * 10 and 0 < work <= budget,
                    'candidate marked applied on an invalid/fallback path')
            applied_layers.add(layer)
            applied_count += 1
            singleton_count += singleton
        elif fallback:
            require(tokens > 1 and reason == 'budget' and
                    work == budget == 32 * tokens * 10,
                    'unknown partition fallback reason')
            fallback_count += 1
        else:
            require(reason in ('decode', 'unsupported') and
                    (reason != 'decode' or (tokens == chunks == singleton == 1
                     and work == budget == metadata == 0)),
                    'unexplained unapplied partition forward')
        reasons[reason] = reasons.get(reason, 0) + 1
    eligibility = {
        'all_layers_have_applied_prefill': applied_layers == set(range(48)),
        'no_budget_fallback': fallback_count == 0,
        'no_unsupported_forwards': reasons.get('unsupported', 0) == 0,
    }
    return {'partition': 1, 'activation_observed': True,
            'candidate_forwards': applied_count,
            'applied_prefill_layers': sorted(applied_layers),
            'fallback_forwards': fallback_count, 'reasons': reasons,
            'runtime_eligible': all(eligibility.values()),
            'eligibility_checks': eligibility,
            'singleton_subchunks': singleton_count,
            'scope': 'runtime selection/shape; no request I/O attribution',
            'numerical_acceptance': False}
