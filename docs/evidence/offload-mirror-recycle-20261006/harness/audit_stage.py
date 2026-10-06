"""Audit one frozen mirror-recycle HTTP group without another request or test."""
import argparse
import hashlib
import math
from pathlib import Path
import re
import sys
import time

from recycle_common import R, W, frozen, read, save, sha

parser = argparse.ArgumentParser()
parser.add_argument('group')
parser.add_argument('--plan-sha256', required=True)
args = parser.parse_args()
assert re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', args.group)
output = R / (args.group + '-decision.json')
assert not output.exists(), 'first audit record already exists'
sources = {}
report = {'schema': 1, 'passed': False, 'group_id': args.group,
          'plan_sha256': args.plan_sha256, 'started_t': time.time(),
          'failure': None, 'source_sha256': sources,
          'performance_acceptance': False, 'numerical_acceptance': False}


def bound(path):
    path = Path(path)
    digest = sha(path)
    assert str(path) not in sources or sources[str(path)] == digest, path
    sources[str(path)] = digest
    return read(path)


def require(condition, message):
    if not condition:
        raise ValueError(message)


try:
    plan = frozen(args.plan_sha256)
    require(args.group in plan['group_ids'], 'group not in frozen plan')
    groups = {group['id']: group for group in plan['groups']}
    require(len(groups) == len(plan['groups']), 'duplicate frozen group id')
    group = groups[args.group]
    kind, state = group['kind'], group['arm']
    require(kind in ('quality', 'history', 'matrix') and
            type(state) is int and state in (0, 1), 'invalid group kind/state')
    Q = W / '.q4t-work/evidence' / args.group
    sys.path.insert(0, str(W / 'tools/evalscope'))
    from offload_policy import check_policy_protocol
    from request_policy_protocol import (request_path_evidence,
        sequence_output_evidence, sequence_plan)
    from mirror_recycle_protocol import mirror_recycle_evidence

    phase = bound(plan['protocol_plan_path'])
    require(phase['http_count'] == 131 and phase['service_count'] == 17,
            'frozen bounded request count changed')
    controller = bound(R / (args.group + '-stage.json'))
    controller_exit = bound(R / (args.group + '-controller-exit.json'))
    require(controller['returncode'] == 0 and controller['failure'] is None and
            controller['cleanup_complete'] is True and
            controller_exit['returncode'] == 0 and
            controller_exit['failure'] is None and
            controller_exit['cleanup_complete'] is True,
            'controller failed or cleanup incomplete')
    require(controller['runtime_binary_sha256'] == plan['runtime_binary_sha256'] and
            controller['runtime_source_commit'] == plan['runtime_source_commit'] and
            controller['plan_sha256'] == args.plan_sha256 and
            controller['group_id'] == args.group and
            controller['first_runtime_test'] is (kind == 'quality'),
            'controller identity or first-test scope mismatch')
    wrapper = bound(Q / 'wrapper-exit.json')
    exit_record = bound(Q / 'http/exit.json')
    process_group = bound(Q / 'runner-process-group.json')
    cleanup = bound(Q / 'http/isolation/cleanup.json')
    capacity = bound(Q / 'http/capacity.json')
    protocol = bound(Q / 'protocol.json')
    command = bound(Q / 'http/server-command.json')
    require(wrapper['runner_rc'] == wrapper['monitor_rc'] ==
            exit_record['server'] == 0 and wrapper['failure'] is None and
            exit_record['failure'] is exit_record['cleanup_failure'] is None and
            wrapper['cleanup_failed'] is False and
            wrapper['unit_after_cleanup']['LoadState'] == 'not-found' and
            exit_record['http_output_checks_passed'] is True and
            not exit_record.get('diagnostic_scope'),
            'HTTP/wrapper terminal contract failed')
    require(process_group['cleanup_complete'] and process_group['runner_reaped'] and
            process_group['returncode'] == 0 and process_group['failure'] is None and
            process_group['after_cleanup']['absent'] and
            all(not process_group['after_cleanup'][key] for key in
                ('live_pids', 'zombie_pids', 'errors')) and
            cleanup['stop_rc'] == 0 and cleanup['unit_removed'] and
            cleanup['properties_after']['MainPID'] == '0',
            'owned process or service cleanup failed')
    require(capacity['matches_requested'] and
            capacity['requested'] == capacity['effective'] ==
            {'max_len': 262144, 'max_seq': 1, 'max_prefill': 8192},
            'server capacity changed or was reduced')
    check_policy_protocol(protocol, state, 'mirror-recycle')
    for path, digest in protocol['input_config_sha256'].items():
        require(plan['frozen_files'].get(path) == digest,
                'input/config identity not bound by execution plan: ' + path)
    require(protocol['binary'] == plan['runtime_binary_path'],
            'wrapper binary path differs')
    require(protocol['effective_environment'] == command['effective_q4t_environment'],
            'actual server environment differs from wrapper')
    require(protocol['binary_sha256'] == plan['runtime_binary_sha256'] and
            sha(plan['runtime_binary_path']) == plan['runtime_binary_sha256'] and
            (Q / 'http/binary.sha256').read_text().strip() ==
            plan['runtime_binary_sha256'] and
            (Q / 'http/commit.txt').read_text().strip() == plan['runtime_source_commit'] and
            not (Q / 'http/worktree.patch').read_bytes(),
            'binary/source/worktree identity differs')
    for name in ('binary.sha256', 'commit.txt', 'worktree.patch'):
        sources[str(Q / 'http' / name)] = sha(Q / 'http' / name)
    require(command['argv'][:2] == [plan['runtime_binary_path'], 'serve'] and
            command['argv'].count('--no-mtp') == 1 and '--mtp' not in command['argv'],
            'actual serve binary or MTP mode differs')
    from offload_policy import RUN_TOOLS
    expected_tools = set(RUN_TOOLS + ('offload_policy.py',
                                      'mirror_recycle_protocol.py'))
    if kind == 'history':
        expected_tools.add('request_policy_protocol.py')
    require(set(protocol['tool_sha256']) == expected_tools,
            'wrapper tool set differs from stage scope')
    for name, digest in protocol['tool_sha256'].items():
        require(digest == plan['tool_sha256'][name] == sha(Q / 'tools' / name),
                'runner tool identity differs: ' + name)
        sources[str(Q / 'tools' / name)] = digest
    result = bound(Q / 'http/results.json')
    rows, metrics = [], []
    output_contract = None
    if kind == 'quality':
        reference = bound(plan['quality_reference_path'])
        require(sha(plan['quality_reference_path']) == plan['quality_reference_sha256'],
                'quality oracle identity differs')
        rows = bound(Q / 'http/quality/responses.json')
        require(len(result) == len(rows) == len(reference) == group['requests'] == 11,
                'fixed quality request count differs')
        require(protocol['host_cache_max_bytes'] is None and
                protocol['swap_max_bytes'] == 0, 'quality resource protocol differs')
        refs = {row['prompt_sha256']: row for row in reference}
        require(len(refs) == 11, 'quality oracle prompt duplicate')
        for row in rows:
            ref = refs[row['prompt_sha256']]
            require(row['success'] and row['response_id_valid'] and
                    row['timing']['within_client_boundaries'] and
                    all(row[key] == ref[key] for key in
                        ('text', 'actual_input', 'actual_output', 'finish')),
                    'quality output/usage/timing differs from fixed oracle')
        require(all(row['success'] and row['exact_match'] and row['length_match']
                    for row in result), 'quality aggregate failed')
    else:
        gate = bound(Q / 'cache-gate.json')
        require(gate['cold_payload_established'] and
                gate['payload_resident_bytes'] == 0 and not gate['advice_errors'] and
                gate['max_advice_rounds'] == protocol['cold_advice_rounds'] == 2 and
                gate['pre_service_only'] and gate['inference_retry'] is False and
                1 <= len(gate['rounds']) <= 2, 'cold payload gate failed')
        for index, summary in enumerate(gate['rounds'], 1):
            round_record = bound(Q / f'cache-advice-round-{index:02d}.json')
            require(all(round_record[key] == value for key, value in summary.items())
                    and summary['round'] == index and not summary['advice_errors'],
                    'cold advice round identity differs')
            if index < len(gate['rounds']):
                require(summary['cold_payload_established'] is False,
                        'unneeded extra cold advice after success')
        final_cache = bound(Q / 'cache-after-advice.json')
        require(final_cache == round_record['observation'] and
                final_cache['complete_file_set_observed'] and
                len(final_cache['files']) == len(protocol['model_files']) == 226 and
                sum(item['resident_bytes'] for item in final_cache['files']
                    if Path(item['path']).suffix in ('.safetensors', '.bin')) == 0,
                'final cold file observation incomplete or nonzero')
        require(protocol['host_cache_max_bytes'] == 16 << 30 and
                protocol['swap_max_bytes'] == 0, 'performance resource protocol differs')
        if kind == 'history':
            history = sequence_plan()
            require(history['sequence'] == phase['history']['sequence'] and
                    history['rounds'] == phase['history']['rounds'] == 3 and
                    protocol['request_policy_sequence'] is True,
                    'history sequence differs')
            cases = history['requests']
        else:
            length = group['input_tokens']
            require(length in phase['matrix']['lengths'] and
                    group['output_tokens'] == (257 if length == 261887 else 256) and
                    protocol['request_policy_sequence'] is False and
                    protocol['lengths'] == [length] and protocol['repeats'] == 3,
                    'single-tier matrix selection differs')
            cases = [dict(case='context-' + str(length), input_tokens=length,
                          max_tokens=group['output_tokens'], repeats=3)]
        require(len(result) == len(cases), 'HTTP case count differs')
        reference = bound(plan['performance_reference_path'])
        require(sha(plan['performance_reference_path']) ==
                plan['performance_reference_sha256'], 'fixed output oracle changed')
        historical = {row['length']: row for row in reference}
        require(len(historical) == 6, 'output oracle needs six original tiers')
        for item, expected in zip(result, cases):
            raw = bound(Q / 'http' / expected['case'] / 'responses.json')
            require(len(raw) == expected['repeats'] and
                    item['length'] == expected['input_tokens'], 'case repeat or length differs')
            if kind == 'history':
                require(all(item[key] == expected[key] for key in
                            ('case', 'round', 'position', 'input_tokens')),
                        'history case order/position differs')
            hashes = [hashlib.sha256(row['text'].encode()).hexdigest() for row in raw]
            require(item['outputs'] == hashes, 'HTTP output digests differ')
            length = expected['input_tokens']
            if length in historical:
                old = historical[length]
                require(old['outputs'] and len(set(old['outputs'])) == 1 and
                        all(row['prompt_sha256'] == old['prompt_sha256'] and
                            digest == old['outputs'][0] for row, digest in zip(raw, hashes)),
                        'output/prompt differs from frozen historical oracle')
            for repeat, row in enumerate(raw, 1):
                require(row['success'] and row['response_id_valid'] and
                        row['timing']['within_client_boundaries'] and
                        row['actual_input'] == expected['input_tokens'] and
                        row['actual_output'] == expected['max_tokens'] and
                        row['finish'] == ['length'], 'HTTP output/usage/timing invalid')
                ttft = row['ttft']
                decode = (row['actual_output'] - 1) / (row['latency'] - ttft)
                require(all(math.isfinite(value) and value > 0 for value in (ttft, decode)),
                        'nonpositive or nonfinite HTTP timing')
                metrics.append({**{key: expected[key] for key in
                    ('case', 'input_tokens', 'round', 'position') if key in expected},
                    'repeat': repeat, 'ttft': ttft, 'decode_tps': decode})
            require(item['deterministic'] and
                    item['prompt_sha256'] == raw[0]['prompt_sha256'] and
                    item['metrics'] == [{'ttft': row['ttft'], 'decode_tps':
                        (row['actual_output'] - 1) / (row['latency'] - row['ttft'])}
                        for row in raw], 'aggregate timing/determinism differs')
            rows.extend(raw)
        require(len(rows) == group['requests'] == (21 if kind == 'history' else 3),
                'performance group request count differs')
        if kind == 'history':
            output_contract = sequence_output_evidence(rows)
    require(len({row['response_id'] for row in rows}) == len(rows),
            'HTTP response id duplicated')
    log_path = Q / 'http/server.log'
    sources[str(log_path)] = sha(log_path)
    log = log_path.read_text()
    require('[q4t][offload_diag]' not in log, 'unexpected phase diagnostic log')
    runtime = request_path_evidence(log, rows, 0)
    recycle = mirror_recycle_evidence(log, rows, state)
    require(runtime['runtime_eligible'] and recycle['passed'], 'runtime path invalid')
    report.update(passed=True, status=('PASS_FIXED_HTTP_11' if kind == 'quality'
                  else 'PASS_GROUP_CONTRACTS'), kind=kind, arm='B' if state else 'A',
        mirror_gpu_recycle=state, runtime_source_commit=plan['runtime_source_commit'],
        runtime_binary_sha256=plan['runtime_binary_sha256'],
        ended_t=controller['ended_t'], runtime_path=runtime, recycle=recycle,
        output_contract=output_contract, request_count=len(rows), metrics=metrics,
        requests=[{key: row[key] for key in ('response_id', 'prompt_sha256',
                  'text', 'actual_input', 'actual_output', 'finish')} for row in rows],
        full_matrix_completed=False, pair_output_comparison_pending=kind != 'quality')
except BaseException as error:
    report.update(status='FAIL_GROUP_AUDIT',
                  failure=type(error).__name__ + ': ' + str(error))
    raise
finally:
    report['recorded_t'] = time.time()
    sources[str(Path(__file__).resolve())] = sha(__file__)
    save(output, report)
    print({key: report.get(key) for key in
           ('group_id', 'status', 'passed', 'request_count', 'failure')}, flush=True)
