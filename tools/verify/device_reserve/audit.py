"""Verify sealed full-hot startup evidence without adding samples."""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools/verify/vmm_compare'))
from trace_audit import audit as audit_trace
from reservation_audit import audit_success

BASE = ROOT / '.q4t-work/device-reserve-20261020'
DEFAULT = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    return json.loads(path.read_text())


def audit_stage(base):
    base = base.resolve(strict=True)
    manifest = read(base / 'sealed-files.json')
    required = {'state.json', 'lease/exit.json', 'kernel/exit.json',
        'resources/exit.json', 'build.json',
        'driver-identity/driver_identity.json', 'run/command.json',
        'plan/plan.json', 'run/normal/protocol.json',
        'run/normal/http/server-command.json', 'run/cache-expected.json',
        'run/cache-ready.json', 'run/cache-launch.json',
        'lease/nvml-queries.jsonl', 'run/normal/http/server.log'}
    assert required <= manifest.keys()
    for rel, digest in manifest.items():
        path = base / rel
        assert path.resolve().is_relative_to(base.resolve()), rel
        assert sha(path) == digest, rel
    state = read(base / 'state.json')
    guard = read(base / 'lease/exit.json')
    assert guard['compute_backend'] == 'nvml'
    kernel = read(base / 'kernel/exit.json')
    resource = read(base / 'resources/exit.json')
    build = read(base / 'build.json')
    binary = BASE / 'candidate/q4t'
    assert state['default_unchanged'] and sha(ROOT / 'build/q4t') == DEFAULT
    assert state['scope_final']['after']['LoadState'] == 'not-found'
    assert 'cleanup_error' not in guard
    assert kernel['status'] == resource['status'] == 'COMPLETED'
    assert kernel['instance_removed'] and kernel['global_configuration_equal']
    assert not any(kernel['loss_totals'].values())
    assert not resource['failures']
    assert read(base / 'driver-identity/driver_identity.json')['attribution_valid']
    assert build['candidate_sha256'] == sha(binary) == state['observer_sha']
    command = read(base / 'run/command.json')
    assert command[command.index('--binary') + 1] == str(binary)
    assert '--clear-model-cache' not in command
    plan = read(base / 'plan/plan.json')
    original_stage = Path(plan['source_stage'])
    original_manifest = read(original_stage / 'sealed-files.json')
    assert sha(original_stage / 'sealed-files.json') == plan['source_seal']['manifest_sha256']
    protocol_rel = 'run/normal/protocol.json'
    assert sha(original_stage / protocol_rel) == original_manifest[protocol_rel]
    original_protocol = read(original_stage / protocol_rel)
    protocol = read(base / protocol_rel)
    for key in ('host_cache_max_bytes', 'swap_max_bytes', 'chunk_order',
                'effective_environment', 'max_len', 'max_seq', 'max_prefill',
                'mode', 'output_tokens', 'monitor'):
        assert protocol[key] == original_protocol[key], key
    actual_server = read(base / 'run/normal/http/server-command.json')['argv']
    expected_server = plan['source_command']['argv'].copy()
    expected_server[0] = str(binary)
    expected_server = [arg.replace(str(original_stage / 'run'), str(base / 'run'))
                       for arg in expected_server]
    assert actual_server == expected_server
    expected = read(base / 'run/cache-expected.json')
    for name in ('cache-ready.json', 'cache-launch.json'):
        cache = read(base / 'run' / name)
        assert cache['complete_file_set_observed']
        actual = {row['path']: row['resident_bytes'] for row in cache['files']
                  if Path(row['path']).suffix in ('.safetensors', '.bin')}
        assert actual == expected, name
    assert sum(expected.values()) == 67987791872
    queries = [json.loads(line) for line in
               (base / 'lease/nvml-queries.jsonl').read_text().splitlines()]
    assert queries
    result = dict(stage=base.name, sealed_files=len(manifest),
        manifest_sha256=sha(base / 'sealed-files.json'),
        guard_status=guard['status'], full_hot_bytes=sum(expected.values()),
        exact_cache_vector=True, nvml_status_counts=dict(collections.Counter(
            row['status'] for row in queries)),
        default_unchanged=True, scope_gone=True, trace_loss=kernel['loss_totals'],
        trace_instance_removed=True, runtime_accepted=False,
        performance_acceptance=False)
    log = base / 'run/normal/http/server.log'
    trace = audit_trace(log)
    result['logical_trace'] = {key: value for key, value in trace.items()
                               if key != 'normalized'}
    if guard['status'] == 'COMPLETED':
        assert {'run/normal/http/exit.json',
                'run/normal/http/results.json'} <= manifest.keys()
        assert state['status'] == 'RUNTIME_COMPLETED'
        assert guard['child_returncode'] == state['guard_returncode'] == 0
        assert all(row['status'] == 'OK' for row in queries)
        exit_info = read(base / 'run/normal/http/exit.json')
        assert exit_info['server'] == 0
        assert exit_info['http_output_checks_passed'] and exit_info['completed'] == 1
        assert exit_info['failure'] is None and exit_info['cleanup_failure'] is None
        rows = read(base / 'run/normal/http/results.json')
        assert len(rows) == 1 and rows[0]['exact_match'] and rows[0]['length_match']
        assert rows[0]['actual_input'] == 1024 and rows[0]['text'] == '710003'
        assert not trace['errors'] and not trace['failed_calls']
        assert not trace['incomplete_calls']
        original_path = Path(plan['source_stage']) / 'run/normal/http/server.log'
        assert sha(original_path) == plan['source_seal']['inputs_sha256']['run/normal/http/server.log']
        original = audit_trace(original_path)
        assert trace['normalized'] == original['normalized']
        result['http'] = rows[0]
        result['logical_lifecycle_matches_capture'] = True
        result['reservation'] = audit_success(log.read_text().splitlines(),
            sha(base / 'plan/plan.json'),
            [row['bytes'] for row in plan['startup_device_allocations']])
        result['runtime_accepted'] = True
    else:
        result['reason'] = guard.get('reason')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    assert not args.output.resolve().is_relative_to(args.stage.resolve())
    assert not args.output.exists()
    result = audit_stage(args.stage)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: value for key, value in result.items()
                      if key != 'logical_trace'}))


if __name__ == '__main__':
    main()
