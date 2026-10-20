"""Recheck the sealed two-stage reliability diagnosis without new sampling."""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / '.q4t-work/warm-reclaim-20261020'


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(1 << 20), b''):
            digest.update(data)
    return digest.hexdigest()


def read(path):
    return json.loads(path.read_text())


def audit_stage(stage):
    base = BASE / stage
    manifest = read(base / 'sealed-files.json')
    for rel, digest in manifest.items():
        assert sha(base / rel) == digest, rel
    state = read(base / 'state.json')
    guard = read(base / 'lease/exit.json')
    kernel = read(base / 'kernel/exit.json')
    resource = read(base / 'resources/exit.json')
    identity = read(base / 'driver-identity/driver_identity.json')
    assert state['status'] == 'DIAGNOSIS_CAPTURED'
    assert state['default_unchanged']
    assert guard['status'] == 'ENV_ABORT' and 'NVML process query' in guard['reason']
    assert 'cleanup_error' not in guard
    assert state['scope_final']['after']['LoadState'] == 'not-found'
    assert kernel['status'] == resource['status'] == 'COMPLETED'
    assert kernel['instance_removed'] and kernel['global_configuration_equal']
    assert not any(kernel['loss_totals'].values())
    assert not resource['failures']
    assert identity['attribution_valid']
    build = read(base / 'build.json')
    observer = ROOT / '.q4t-work/vmm-compare-20261020/runtime/q4t'
    assert build['arms']['runtime']['binary'] == sha(observer)
    command = read(base / 'run/command.json')
    assert command[command.index('--binary') + 1] == str(observer)
    assert '--clear-model-cache' not in command
    expected = read(base / 'run/cache-expected.json')
    for name in ('cache-ready.json', 'cache-launch.json'):
        cache = read(base / 'run' / name)
        assert cache['complete_file_set_observed']
        actual = {row['path']: row['resident_bytes'] for row in cache['files']
                  if Path(row['path']).suffix in ('.safetensors', '.bin')}
        assert actual == expected, name
    assert sum(expected.values()) == 67987791872
    assert not (base / 'run/normal/http/exit.json').exists()
    assert '[memory-trace]' in (base / 'run/normal/http/server.log').read_text()
    queries = [json.loads(line) for line in
               (base / 'lease/nvml-queries.jsonl').read_text().splitlines()]
    bad = [row for row in queries if row['status'] != 'OK']
    assert len(bad) == 1 and bad[0]['status'] == 'CLEANUP_FAILED'
    worker = [json.loads(line) for line in bad[0]['stdout'].splitlines()]
    begins = [row for row in worker if row.get('event') == 'begin']
    end_apis = [row.get('api') for row in worker if row.get('event') == 'end']
    result = dict(stage=stage, sealed_files=len(manifest),
        manifest_sha256=sha(base / 'sealed-files.json'), guard_status=guard['status'],
        model_loads=1, http_requests=0, full_hot_bytes=sum(expected.values()),
        exact_cache_vector=True, nvml_status_counts=dict(collections.Counter(
            row['status'] for row in queries)), last_nvml_api=begins[-1]['api'],
        completed_nvml_apis=end_apis,
        trace_bytes=kernel['trace_bytes'], trace_loss=kernel['loss_totals'],
        instance_removed=kernel['instance_removed'],
        global_trace_configuration_equal=kernel['global_configuration_equal'],
        system_samples=resource['system_samples'], cache_samples=resource['cache_samples'],
        capture_seconds=kernel['ended_monotonic_ns']/1e9-kernel['started_monotonic_ns']/1e9,
        default_unchanged=True, scope_gone=True,
        trace_sha256=sha(base / 'kernel/trace.txt'))
    if (base / 'run/compaction/state.json').exists():
        result['precompaction'] = read(base / 'run/compaction/state.json')
    return result, expected


def main(output):
    rows = []
    vectors = []
    for stage in ('baseline-v1', 'precompact-v1'):
        row, vector = audit_stage(stage)
        rows.append(row)
        vectors.append(vector)
    assert vectors[0] == vectors[1]
    result = dict(status='BOUNDED_DIAGNOSIS_COMPLETE_NO_FULL_WARM_FIX',
        stages=rows, total_model_loads=2, total_http_requests=0,
        total_stage_sealed_files=sum(row['sealed_files'] for row in rows),
        default_sha256=sha(ROOT / 'build/q4t'),
        evidence_valid_for='observed mechanism and failed precompaction contract',
        runtime_acceptance=False, performance_acceptance=False,
        limitations=['Fixed stage order; driver pools and physical state differ.',
                     'No PSI in running kernel; never interpreted as zero pressure.',
                     'No complete driver lock-owner proof or pool order mapping.',
                     'No claim that all KReclaimable belongs to NVIDIA.'])
    assert result['default_sha256'] == '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'
    final = BASE / 'final-evidence.json'
    if final.exists():
        manifest = read(final)
        for rel, digest in manifest.items():
            assert sha(BASE / rel) == digest, rel
        result['all_stage_and_tool_evidence'] = dict(files=len(manifest),
            manifest_path=str(final), manifest_sha256=sha(final))
        tests = (BASE / 'host-tests-delivery.log').read_text()
        assert tests.rstrip().endswith('OK')
        result['host_contracts_passed'] = int(re.search(r'Ran (\d+) tests', tests)[1])
        result['analysis'] = {}
        for stage, key in [('baseline-v1', 'baseline'), ('precompact-v1', 'precompact')]:
            items = {}
            for kind, directory in [('kernel', 'analysis-final-verified'),
                                    ('resources', 'comparison-resources-final-units')]:
                path = BASE / directory / (key + '.json')
                doc = read(path)
                items[kind] = dict(path=str(path), sha256=sha(path))
                if kind == 'kernel':
                    items[kind].update(events=doc['event_count'],
                                      verified_inputs=doc['sealed_inputs_verified'])
            result['analysis'][stage] = items
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ('status', 'total_model_loads',
          'total_http_requests', 'total_stage_sealed_files')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    main(parser.parse_args().output)
