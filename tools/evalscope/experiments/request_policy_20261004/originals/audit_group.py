"""Read-only audit of a completed HTTP group, with no extra requests."""
import argparse
import hashlib
import math
from pathlib import Path
import sys
import time

from phase_common import R, W, frozen, read, save, sha

parser = argparse.ArgumentParser()
parser.add_argument('stage')
parser.add_argument('--plan-sha256', required=True)
args = parser.parse_args()
plan = frozen(args.plan_sha256)
sys.path.insert(0, str(W / 'tools/evalscope'))
from offload_policy import check_policy_protocol
from request_policy_protocol import (all_layer_partition_evidence,
    request_path_evidence, sequence_output_evidence, sequence_plan)

stage = args.stage
state = int(stage.endswith('-on'))
Q = W / '.q4t-work/evidence' / stage
sources = {}


def bound(path):
    sources[str(path)] = sha(path)
    return read(path)


controller = bound(R / (stage + '-stage.json'))
assert controller['returncode'] == 0 and controller['failure'] is None
wrapper = bound(Q / 'wrapper-exit.json')
exit_record = bound(Q / 'http/exit.json')
group = bound(Q / 'runner-process-group.json')
cleanup = bound(Q / 'http/isolation/cleanup.json')
capacity = bound(Q / 'http/capacity.json')
protocol = bound(Q / 'protocol.json')
command = bound(Q / 'http/server-command.json')
assert wrapper['runner_rc'] == wrapper['monitor_rc'] == exit_record['server'] == 0
assert wrapper['failure'] is exit_record['failure'] is exit_record['cleanup_failure'] is None
assert not wrapper['cleanup_failed']
assert wrapper['unit_after_cleanup']['LoadState'] == 'not-found'
assert exit_record['http_output_checks_passed']
assert not exit_record.get('diagnostic_scope')
assert group['cleanup_complete'] and group['runner_reaped']
assert group['returncode'] == 0 and group['failure'] is None
assert group['after_cleanup']['absent']
assert all(not group['after_cleanup'][k] for k in ['live_pids', 'zombie_pids', 'errors'])
assert cleanup['stop_rc'] == 0 and cleanup['unit_removed']
assert cleanup['properties_after']['MainPID'] == '0'
assert capacity['matches_requested']
assert capacity['requested'] == capacity['effective'] == {
    'max_len': 262144, 'max_seq': 1, 'max_prefill': 8192}
check_policy_protocol(protocol, state, 'request-partition')
assert protocol['effective_environment'] == command['effective_q4t_environment']
assert protocol['binary_sha256'] == plan['runtime_binary_sha256']
assert sha(plan['runtime_binary_path']) == plan['runtime_binary_sha256']
assert (Q / 'http/binary.sha256').read_text().strip() == plan['runtime_binary_sha256']
assert (Q / 'http/commit.txt').read_text().strip() == plan['runtime_source_commit']
assert not (Q / 'http/worktree.patch').read_bytes()
assert command['argv'][:2] == [plan['runtime_binary_path'], 'serve']
assert command['argv'].count('--no-mtp') == 1 and '--mtp' not in command['argv']
for name, digest in protocol['tool_sha256'].items():
    assert digest == plan['tool_sha256'][name] == sha(Q / 'tools' / name)
result = bound(Q / 'http/results.json')
rows = []
metrics = []
output_contract = None
if stage == 'quality-on':
    reference = bound(plan['quality_reference_path'])
    rows = bound(Q / 'http/quality/responses.json')
    assert len(result) == len(rows) == len(reference) == 11
    refs = {r['prompt_sha256']: r for r in reference}
    assert len(refs) == 11
    for row in rows:
        ref = refs[row['prompt_sha256']]
        assert row['success'] and row['response_id_valid']
        assert row['timing']['within_client_boundaries']
        assert all(row[k] == ref[k] for k in ['text', 'actual_input', 'actual_output', 'finish'])
    assert all(r['success'] and r['exact_match'] and r['length_match'] for r in result)
else:
    gate = bound(Q / 'cache-gate.json')
    assert gate['cold_payload_established'] and gate['payload_resident_bytes'] == 0
    assert not gate['advice_errors']
    assert gate['max_advice_rounds'] == protocol['cold_advice_rounds'] == 2
    assert gate['pre_service_only'] and gate['inference_retry'] is False
    assert 1 <= len(gate['rounds']) <= 2
    for index, summary in enumerate(gate['rounds'], 1):
        round_record = bound(Q / f'cache-advice-round-{index:02d}.json')
        assert all(round_record[k] == v for k, v in summary.items())
        assert summary['round'] == index and not summary['advice_errors']
        if index < len(gate['rounds']):
            assert summary['cold_payload_established'] is False
    final_cache = bound(Q / 'cache-after-advice.json')
    assert final_cache == round_record['observation']
    assert final_cache['complete_file_set_observed'] and len(final_cache['files']) == 226
    assert sum(r['resident_bytes'] for r in final_cache['files']
               if Path(r['path']).suffix in ('.safetensors', '.bin')) == 0
    assert protocol['host_cache_max_bytes'] == 16 << 30 and protocol['swap_max_bytes'] == 0
    inherited = stage.startswith('inheritance-')
    cases = sequence_plan()['requests'] if inherited else [
        {'case': 'context-' + str(n), 'input_tokens': n,
         'max_tokens': 257 if n == 261887 else 256, 'repeats': 3}
        for n in plan['matrix_lengths']]
    assert len(result) == len(cases)
    reference = bound(plan['performance_reference_path'])
    assert sha(plan['performance_reference_path']) == plan['performance_reference_sha256']
    historical = {r['length']: r for r in reference}
    assert len(historical) == 6
    for item, expected in zip(result, cases):
        raw = bound(Q / 'http' / expected['case'] / 'responses.json')
        assert len(raw) == expected['repeats']
        assert item['length'] == expected['input_tokens']
        if inherited:
            assert all(item[k] == expected[k]
                       for k in ('case', 'round', 'position', 'input_tokens'))
        assert item['outputs'] == [hashlib.sha256(r['text'].encode()).hexdigest()
                                   for r in raw]
        length = expected['input_tokens']
        if length in historical:
            old = historical[length]
            assert old['outputs'] and len(set(old['outputs'])) == 1
            assert all(r['prompt_sha256'] == old['prompt_sha256'] and
                       hashlib.sha256(r['text'].encode()).hexdigest() == old['outputs'][0]
                       for r in raw)
        if state:
            off = W / '.q4t-work/evidence' / stage.removesuffix('-on')
            off = off.with_name(off.name + '-off')
            paired = bound(off / 'http' / expected['case'] / 'responses.json')
            assert len(paired) == len(raw)
            assert all(all(a[k] == b[k] for k in
                           ('text', 'prompt_sha256', 'actual_input', 'actual_output', 'finish'))
                       for a, b in zip(raw, paired))
        for row in raw:
            assert row['success'] and row['response_id_valid']
            assert row['timing']['within_client_boundaries']
            assert row['actual_input'] == expected['input_tokens']
            assert row['actual_output'] == expected['max_tokens'] and row['finish'] == ['length']
            ttft = row['ttft']
            decode = (row['actual_output'] - 1) / (row['latency'] - ttft)
            assert all(math.isfinite(v) and v > 0 for v in (ttft, decode))
            assert hashlib.sha256(row['text'].encode()).hexdigest() in item['outputs']
            metrics.append({**{k: expected[k] for k in
                ('case', 'input_tokens', 'round', 'position') if k in expected},
                'ttft': ttft, 'decode_tps': decode})
        assert item['deterministic']
        assert item['prompt_sha256'] == raw[0]['prompt_sha256']
        assert item['metrics'] == [{'ttft': r['ttft'], 'decode_tps':
            (r['actual_output'] - 1) / (r['latency'] - r['ttft'])} for r in raw]
        rows.extend(raw)
    assert len(rows) == (21 if inherited else 18)
    if inherited:
        output_contract = sequence_output_evidence(rows)
assert len({r['response_id'] for r in rows}) == len(rows)
log_path = Q / 'http/server.log'
sources[str(log_path)] = sha(log_path)
log = log_path.read_text()
assert '[q4t][offload_diag]' not in log
runtime = request_path_evidence(log, rows, state)
all_layers = all_layer_partition_evidence(log, rows, state)
assert runtime['runtime_eligible'] and all_layers['runtime_eligible']
report = {'schema': 1, 'passed': True, 'stage': stage,
    'plan_sha256': args.plan_sha256,
    'status': 'PASS_FIXED_HTTP_11' if stage == 'quality-on' else 'PASS_GROUP_CONTRACTS',
    'ended_t': controller['ended_t'], 'recorded_t': time.time(),
    'runtime_binary_sha256': plan['runtime_binary_sha256'],
    'runtime_source_commit': plan['runtime_source_commit'],
    'source_sha256': sources, 'runtime_path': runtime,
    'all_layer_runtime': all_layers, 'output_contract': output_contract,
    'request_count': len(rows),
    'metrics': metrics, 'performance_acceptance': False, 'numerical_acceptance': False}
save(R / (('quality' if stage == 'quality-on' else stage) + '-decision.json'), report)
print({k: v for k, v in report.items() if k not in ('source_sha256', 'metrics')})
