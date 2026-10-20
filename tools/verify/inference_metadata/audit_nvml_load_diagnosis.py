"""Offline checks of bounded NVML/load diagnosis, including negative evidence."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / '.q4t-work/nvml-load-20261020'


def read(path):
    return json.loads(path.read_text())


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require(value, message):
    if not value:
        raise ValueError(message)


def audit():
    result = {}
    for version, status in ((1, 'ENV_ABORT'), (2, 'COMMAND_FAILED'), (3, 'COMPLETED')):
        stage = BASE / f'diagnostic-v{version}'
        manifest = read(stage / 'sealed-files.json')
        for name, digest in manifest.items():
            path = (stage / name).resolve()
            require(path.is_relative_to(stage) and sha(path) == digest, 'seal: ' + name)
        guard = read(stage / 'lease/exit.json')
        require(guard['status'] == status and guard['compute_backend'] == 'nvml', 'guard status')
        run = stage / 'run'
        source = read(run / 'sources.json')
        for name, digest in source.items():
            require(sha(run / 'source' / name) == digest, 'saved source identity')
            if version == 3:
                require(sha(ROOT / name) == digest, 'current source identity')
        query = rows(stage / 'lease/nvml-queries.jsonl')
        waits = []
        joint = []
        for index, sample in enumerate(rows(run / 'stacks.jsonl')):
            model_compact = False
            nvml_lock = False
            for target in sample['targets']:
                for task in target['tasks']:
                    stack = task['stack']['value'] or ''
                    if target['kind'] == 'model' and 'nv_alloc_system_pages' in stack and 'compact' in stack:
                        model_compact = True
                    if target['kind'] == 'nvml' and 'rmapiLockAcquire' in stack:
                        nvml_lock = True
            if nvml_lock:
                waits.append(index)
            if nvml_lock and model_compact:
                joint.append(dict(index=index, monotonic=sample['monotonic']))
        result[str(version)] = dict(guard=status, sealed_files=len(manifest),
            cache_after_prewarm_bytes=read(run / 'cache-after-prewarm.json')['resident_bytes'],
            prewarm_bytes=read(run / 'prewarm.json')['bytes'],
            query_count=len(query), query_max_seconds=max(q['seconds'] for q in query),
            failed_queries=[dict(status=q['status'], worker_pid=q['worker_pid'],
                seconds=q['seconds']) for q in query if q['status'] != 'OK'],
            nvml_lock_samples=len(waits), simultaneous_compaction_and_lock=joint,
            kernel_allocation_errors=[k for k in rows(run / 'kernel.jsonl')
                                      if 'NV_ERR_NO_MEMORY' in k.get('MESSAGE', '')])
        if version == 1:
            require(joint and result['1']['failed_queries'], 'missing lock/compaction witness')
            require(not result['1']['kernel_allocation_errors'], 'unexpected allocation error')
            require('[startup] model_load' in (run / 'normal/http/server.log').read_text(),
                    'model load completion not observed')
        elif version == 2:
            gate = read(run / 'normal/cache-gate.json')
            require(not gate['cold_payload_established'] and gate['payload_resident_bytes'] == 393216,
                    'original cache preparation failure lost')
            require(not (run / 'normal/http').exists(), 'unexpected second-stage load')
        else:
            require(not result['3']['failed_queries'], 'NVML guard failure')
            gate = read(run / 'normal/cache-gate.json')
            preparation = read(run / 'normal/cache-preparation.json')
            require(gate['cold_payload_established'] and gate['payload_resident_bytes'] == 0 and
                    not gate['advice_errors'] and 1 <= len(preparation['attempts']) <= 3,
                    'strict cold preparation')
            http = read(run / 'normal/http/results.json')
            require(len(http) == 1 and http[0]['success'] == 1 and
                    http[0]['exact_match'] and http[0]['length_match'], 'HTTP quality')
            wrapper = read(run / 'normal/wrapper-exit.json')
            require(wrapper['runner_rc'] == wrapper['monitor_rc'] == 0 and
                    not wrapper['cleanup_failed'] and not wrapper['failure'], 'cleanup')
            require(sha(ROOT / 'build/q4t') == read(run / 'plan.json')['binary_sha256'],
                    'default binary identity')
            result['3']['cache_preparation_passes'] = len(preparation['attempts'])
            result['3']['http_quality_requests'] = 1
    return dict(status='PASS_BOUNDED_DIAGNOSIS', stages=result,
        relationship='NVML driver-lock wait overlaps model allocation/direct compaction; allocation error is not required',
        operating_contract='64GiB memcg; explicit prelaunch targeted model-cache preparation; unchanged NVML deadlines',
        warm_start_fixed=False, driver_internal_root_cause_proven=False,
        performance_acceptance=False, physical_ram_acceptance=False)


if __name__ == '__main__':
    print(json.dumps(audit(), indent=2))
