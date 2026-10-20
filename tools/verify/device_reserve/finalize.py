"""Recheck both sealed starts and summarize the bounded research acceptance."""
import argparse
import json
from pathlib import Path
import re
import shutil

from audit import BASE, ROOT, DEFAULT, audit_stage, read, sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--suffix', default='')
    args = parser.parse_args()
    assert re.fullmatch(r'(?:-v[0-9]+)?', args.suffix)
    assert not args.output.exists()
    assert not args.output.resolve().is_relative_to(BASE.resolve())
    build = read(BASE / 'build.json')
    assert build['status'] == 'COMPLETED'
    assert build['unwrapped_sha256'] == sha(BASE / 'unwrapped') == DEFAULT
    assert sha(ROOT / 'build/q4t') == DEFAULT
    equivalent = None
    source_build = BASE / 'candidate/source'
    if (BASE / 'style/build.json').exists():
        formatted = read(BASE / 'style/build.json')
        equivalent = read(BASE / 'style/equivalence.json')
        assert formatted['status'] == 'COMPLETED'
        assert formatted['candidate_sha256'] == build['candidate_sha256']
        assert formatted['gpu_contract_sha256'] == build['gpu_contract_sha256']
        assert sha(BASE / 'style/candidate/q4t') == build['candidate_sha256']
        assert sha(BASE / 'style/candidate/gpu_contract') == build['gpu_contract_sha256']
        assert sha(BASE / 'style/test_owner') == sha(BASE / 'owner-host-sealed/test_owner')
        source_build = BASE / 'style/candidate/source'
    for name in ('compile.log', 'link.log', 'gpu-contract-compile.log'):
        assert (BASE / 'candidate' / name).read_bytes() == b''
    for name in ('owner.h', 'memory_trace.cpp', 'gpu_contract.cpp'):
        assert sha(Path(__file__).with_name(name)) == sha(source_build / name)
    owner = read(BASE / 'owner-host-sealed/result.json')
    assert owner['build_exit'] == owner['run_exit'] == 0
    assert owner['result'] == {'status': 'PASS', 'host_contracts': 11}
    assert sha(BASE / 'owner-host-sealed/test_owner') == owner['binary_sha256']
    for name, digest in owner['sources'].items():
        if equivalent is None:
            assert sha(ROOT / name) == digest
        else:
            assert sha(ROOT / name) == equivalent['source_after_sha256'][name]
    wrapper = read(BASE / 'wrapper-host-v2/result.json')
    assert wrapper['status'] == 'PASS' and len(wrapper['cases']) == 5
    for name, digest in read(BASE / 'wrapper-host-v2/identity.json')['artifacts'].items():
        assert sha(BASE / 'wrapper-host-v2' / name) == digest
    for folder in ('analysis-host', 'gpu-contract-v1'):
        for name, digest in read(BASE / folder / 'sealed-files.json').items():
            assert sha(BASE / folder / name) == digest
    tests = (BASE / 'analysis-host/stderr.log').read_text()
    assert tests.rstrip().endswith('OK') and re.search(r'Ran 16 tests', tests)
    gpu = read(BASE / 'gpu-contract-v1/result.json')
    lease = read(BASE / 'gpu-contract-v1/lease/exit.json')
    assert gpu['status'] == 'PASS' and gpu['gpu_contracts'] == 3
    assert gpu['binary_sha256'] == build['gpu_contract_sha256'] == sha(BASE / 'candidate/gpu_contract')
    assert lease['status'] == 'COMPLETED' and lease['child_returncode'] == 0
    assert lease['compute_backend'] == 'nvml'
    stages = []
    vectors = []
    for name in ('reserve-v1', 'restart-v1'):
        stage = BASE / name
        audit = audit_stage(stage)
        assert audit['runtime_accepted']
        kernel = read(BASE / ('analysis-' + name + '.json'))
        resources = read(BASE / ('resources-' + name + '.json'))
        assert kernel['status'] == 'PASS' and not kernel['uncompleted_operations']
        assert kernel['sealed_manifest_sha256'] == audit['manifest_sha256']
        assert resources['cache_launch_vector_matches']
        for path, digest in kernel['evidence_sha256'].items():
            assert sha(stage / path) == digest
        current = resources['cgroup_summary']
        assert current['memory_max_bytes'] == [68719476736]
        assert not any(current['memory_events_maxima'].values())
        assert current['maximum_swap_current_bytes'] == 0
        queries = [json.loads(line) for line in (stage / 'lease/nvml-queries.jsonl').read_text().splitlines()]
        allocs = [x for x in kernel['operations'] if x['event'] == 'alloc']
        workspace = [x for x in allocs if x['bytes'] == 2348023808]
        assert len(allocs) == 1397 and len(workspace) == 1
        rows = read(stage / 'run/cache-launch.json')['files']
        vectors.append({x['path']: x['resident_bytes'] for x in rows})
        stages.append(dict(name=name, sealed_files=audit['sealed_files'],
            sealed_manifest_sha256=audit['manifest_sha256'],
            full_hot_bytes=audit['full_hot_bytes'], runtime_accepted=True,
            nvml_queries=len(queries), nvml_max_seconds=max(x['seconds'] for x in queries),
            physical_allocation_seconds=kernel['completed_allocation_seconds'],
            longest_physical_allocation_seconds=max(x['seconds'] for x in allocs),
            workspace_allocation_seconds=workspace[0]['seconds'],
            compaction_seconds_in_allocations=kernel['compaction_seconds_in_completed_allocations'],
            direct_reclaim_seconds_in_allocations=kernel['direct_reclaim_seconds_in_completed_allocations'],
            reservation=audit['reservation'],
            logical_lifecycle_matches_capture=audit['logical_lifecycle_matches_capture'],
            http={k: audit['http'][k] for k in ('actual_input','actual_output','text','ttft','latency','exact_match')},
            cgroup=current, default_unchanged=True, scope_gone=True,
            kernel_analysis_sha256=sha(BASE / ('analysis-' + name + '.json')),
            resource_analysis_sha256=sha(BASE / ('resources-' + name + '.json'))))
    assert vectors[0] == vectors[1]
    assert not (ROOT / '.q4t-work/gpu-exclusive.lock.recovery-required').exists()
    sources = BASE / ('final-tools' + args.suffix)
    sources.mkdir(exist_ok=False)
    for path in Path(__file__).parent.iterdir():
        if path.is_file():
            shutil.copy2(path, sources / path.name)
    final_manifest = BASE / ('final-evidence' + args.suffix + '.json')
    assert not final_manifest.exists()
    manifest = {str(path.relative_to(BASE)): sha(path) for path in BASE.rglob('*')
                if path.is_file()}
    final_manifest.write_text(json.dumps(manifest, indent=2) + '\n')
    for name, digest in manifest.items():
        assert sha(BASE / name) == digest
    result = dict(status='ENGINEERING_ACCEPTED_FIXED_CONFIG_RESEARCH_PROTOTYPE',
        default_sha256=DEFAULT, candidate_sha256=build['candidate_sha256'],
        plan_sha256=build['plan']['sha256'], host_contracts=32, gpu_contracts=3,
        model_loads=2, http_requests=2, stages=stages,
        stage_files_verified=sum(x['sealed_files'] for x in stages),
        final_evidence_files=len(manifest), final_evidence_path=str(final_manifest),
        final_evidence_sha256=sha(final_manifest),
        same_full_launch_vector=True, observed_files=len(vectors[0]),
        default_replaced=False, performance_acceptance=False,
        source_format_equivalence=equivalent,
        scope=dict(mtp=False, resident_slots=256, l2_slots=16, mirror_k=8,
                   max_seq=1,max_len=262144,max_prefill=8192,memcg_max_bytes=68719476736),
        limitations=[
            'Trace-specific fixed configuration prototype; not a general runtime memory planner.',
            'Two bounded starts, not long-duration stability or statistical performance proof.',
            'Full hot vector is the prelaunch condition; file cache changes during loading/inference.',
            'Original post-ready process-static allocations and library allocations remain outside startup owner.',
            'Driver/pool initial state differs between runs; no unique pinned-memory or driver lock root-cause proof.',
            'NVML remained mandatory with its original timeout; later async allocations can still wait.'
        ])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    # Do not print after sealing: callers may redirect stdout into BASE.


if __name__ == '__main__':
    main()
