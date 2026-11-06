"""Record this reviewed QSA adoption decision and preserve its exact evidence.

This is a task-specific receipt, not a general performance tolerance or an
automatic optimizer. All three completed, reviewed stages are prerequisites.
Large full-state buffers remain at their original paths with verified hashes.
"""
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import tarfile

spec = importlib.util.spec_from_file_location('qsa_adoption_acceptance',
    Path(__file__).with_name('adoption.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
OUT = a.BASE / 'acceptance'


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def archive(path, paths, index_path):
    index, objects = {}, {}
    for p in sorted(paths):
        digest = a.sha(p)
        index[str(p)] = dict(sha256=digest, bytes=p.stat().st_size,
                            member='objects/' + digest)
        objects.setdefault(digest, p)
    a.save(index_path, index)
    with tarfile.open(path, 'x:gz', compresslevel=2) as tar:
        tar.add(index_path, arcname='index.json', recursive=False)
        for digest, p in objects.items():
            tar.add(p, arcname='objects/' + digest, recursive=False)
    with tarfile.open(path, 'r:gz') as tar:
        a.require(set(tar.getnames()) == {'index.json'} | {
            'objects/' + s for s in objects}, 'archive member coverage')
        for digest in objects:
            with tar.extractfile('objects/' + digest) as stream:
                a.require(hashlib.file_digest(stream, 'sha256').hexdigest() == digest,
                          'archive content changed')
    return dict(passed=True, archive=str(path), sha256=a.sha(path),
                bytes=path.stat().st_size, paths=len(index), objects=len(objects),
                index_sha256=a.sha(index_path))


def main():
    a.require(not OUT.exists(), 'fresh acceptance receipt')
    plans, reviews, inputs, raw, retained = {}, {}, set(), set(), {}
    for stage in ('http', 'contracts', 'cache'):
        folder = a.BASE / stage
        plan = a.read(folder / 'plan.json')
        plans[stage] = plan
        a.base.monitored_stage.require_complete_monitor(plan)
        a.legacy.verify_files(plan)
        a.require(a.read(folder / 'lease/exit.json')['status'] == 'COMPLETED',
                  'completed outer lease: ' + stage)
        reviews[stage] = a.read(folder / 'review.json')
        inputs.update(Path(p) for p in plan['files'])
        for name, digest in a.read(folder / 'run/files.json').items():
            p = folder / 'run' / name
            a.require(a.sha(p) == digest, 'sealed execution changed: ' + str(p))
        for p in (folder / 'run').rglob('*'):
            if not p.is_file():
                continue
            if {'states', 'session', 'prefix'} & set(p.relative_to(folder / 'run').parts):
                retained[str(p)] = dict(sha256=a.sha(p), bytes=p.stat().st_size,
                                       local_only=True)
            else:
                raw.add(p)
        raw.update(p for p in (folder / 'lease').rglob('*') if p.is_file())
        raw.update(p for p in folder.glob('*.json'))
    h, n, c = [reviews[x] for x in ('http', 'contracts', 'cache')]
    a.require(h['decision'] == 'POSITIVE_TARGET_REQUIRES_CONTRACTS_AND_PROTECTION_REVIEW'
              and h['outputs_identical'] and n['passed'] and c['passed'],
              'all completed adoption contracts')
    a.require(a.sha(a.ORIGINAL / 'build/q4t') == a.DEFAULT and
              a.sha(a.ORIGINAL / 'build/q4t.release.json') == a.SIDECAR,
              'unchanged default before acceptance')
    manifest = a.read(a.PLAN.parent / 'frozen/candidate-runtime-source.json')
    a.require(all(a.sha(a.ROOT / p) == digest for p, digest in manifest.items()),
              'accepted runtime source remains exact')
    old = a.ROOT / '.q4t-work/qsa-softmax-repair-20261106'
    a.require(a.read(old / 'decision.json')['decision'] == 'NO_GO_FEASIBILITY_TARGET',
              'original feasibility decision preserved')
    a.require(a.read(old / 'contract/result.json')['passed'] and
        a.read(old / 'compiled-review.json')['small_t_generic_instructions_identical'],
        'reused direct and compiled contracts')
    for name, digest in a.read(old / 'contract/files.json').items():
        a.require(a.sha(Path(name)) == digest, 'reused numerical dependency')
        inputs.add(Path(name))
    a.require(a.sha(a.ROOT / 'src/model/full_attention.cu') ==
              a.read(old / 'contract/build.json')['sources']['repaired'],
              'original numerical source identity')
    raw.update(old / name for name in ('decision.json', 'compiled-review.json',
        'contract/result.json', 'contract/result.log', 'contract/files.json',
        'contract-lease/exit.json'))
    refresh = a.ROOT / '.q4t-work/reference-refresh-acceptance-20261106'
    original_commit = a.read(refresh / 'plan.json')['source_commit']
    cpu_sources = ['tests/test_main.cpp', 'tests/io_model_config_test.cpp',
        'src/io/model_config.cpp', 'src/io/json.cpp',
        'tools/verify/qsa_subtiming/contract_test.cpp',
        'tools/verify/qsa_subtiming/fake_cuda/cuda_runtime.h']
    cpu_sources += [str(p.relative_to(a.ROOT)) for p in (a.ROOT / 'include').rglob('*.h')]
    for name in cpu_sources:
        original = subprocess.check_output(['git', 'show', original_commit + ':' + name], cwd=a.ROOT)
        a.require(digest_bytes(original) == a.sha(a.ROOT / name), 'CPU reuse source')
        inputs.add(a.ROOT / name)
    for entry in a.read(refresh / 'build-cpu.json'):
        log = refresh / (entry['name'] + '.log')
        a.require(entry['returncode'] == 0 and a.sha(log) == entry['log_sha256'],
                  'CPU/build receipt')
        raw.add(log)
    raw.add(refresh / 'build-cpu.json')
    resources = a.read(a.PLAN.parent / 'resources.json')
    a.require(len(resources['windows']) == 30 and
              all(not w['unavailable'] for w in resources['windows']),
              'complete resource observations')
    OUT.mkdir()
    negative = []
    for head, cells in h['comparisons'].items():
        for case, cell in cells.items():
            if cell['ttft_gain'] < 0 or cell['decode_gain'] < 0 or cell['e2e_change'] > 0:
                p, q = cell['parent'], cell['candidate']
                negative.append(dict(head=head, case=case, metrics=cell,
                    ttft_delta_ms=(q['mean_ttft'] - p['mean_ttft']) * 1000,
                    e2e_delta_ms=(q['aggregate_e2e'] - p['aggregate_e2e']) / 3 * 1000,
                    interpretation='Retained descriptive negative observation; no numeric loss tolerance assigned.'))
    seconds = {stage: a.read(a.BASE / stage / 'run/state.json')['elapsed_seconds'] for stage in plans}
    decision = dict(status='PASS_QSA_BOUNDED_ENGINEERING_ACCEPTANCE', passed=True,
        candidate_sha256=a.CANDIDATE, parent_sha256=a.DEFAULT,
        previous_sidecar_sha256=a.SIDECAR,
        runtime_commit=plans['http']['runtime_commit'], runtime_delta=plans['http']['runtime_delta'],
        runtime_manifest_sha256=a.sha(a.PLAN.parent / 'frozen/candidate-runtime-source.json'),
        reviews={stage: a.sha(a.BASE / stage / 'review.json') for stage in plans},
        resources_sha256=a.sha(a.PLAN.parent / 'resources.json'),
        classification='PERFORMANCE_OPTIMIZATION_NEW_EXECUTION_MAPPING',
        authority='2026-11-06 user approved the separate bounded gradual-adoption plan; not the old deletion-only exception.',
        engineering=dict(decision='ACCEPT_THIS_CANDIDATE_WITH_TIMING_UNCERTAINTY',
            reason='All three 45K target pairs and all 200K pairs improve actual TTFT/E2E; independent arithmetic, full-layer state, HTTP/tool/cache contracts pass. The change retains ordered arithmetic with a small localized kernel delta and a known rollback pair.',
            protection='Every one of the 15 cells is retained. Plain 1K adds 5.117 ms TTFT and 8.702 ms E2E; full 1K adds 2.073 ms E2E. Worst observed decode reduction is full 200K -0.4639%, 36.187 ms per 256-output request. These are not declared noise or allowable loss bounds. The modified large-T kernel is absent from single-stream plain/MTP decode; the optional observer creates no events when off. The evidence does not establish an attributable regression, but cannot exclude a small real loss. Acceptance is specific to this complete bounded record, not averaging away a confirmed regression.',
            cost='768 additional shared bytes and two CTA barriers per tile; registers 56, no stack/local spill; no quantization or changed reduction order.',
            negative_observations=negative),
        comparisons=h['comparisons'],
        statistics='DESCRIPTIVE_SINGLE_START_PER_MODE_IDENTITY_NOT_NONINFERIORITY',
        original_3_percent_gate='UNCHANGED_NO_GO_FEASIBILITY_TARGET',
        original_failure='Preserved FMA failure, four BF16 and 15157 FP32 negative-control differences.',
        contracts=dict(http_generation_requests=184, paired_responses=156, quality_checks=66,
            tool_loops=2, tool_requests=4, cache_requests=24, model_loads=12,
            new_numerical_groups=60, new_bf16_values=25804800, new_fp32_boundaries=65536,
            complete_layers=48, state_pairs=42,
            state_compared_bytes=sum(v['bytes'] for pair in n['pairs'].values() for v in pair.values()),
            reused_cpu_tests=14, reused_numeric_source_unchanged=True),
        budget=dict(actual_stage_seconds=seconds, actual_total_stage_seconds=sum(seconds.values()),
                    approved_models=12, approved_http=184, approved_seconds=9900,
                    conditional_delivery=dict(models=2, http=6, seconds=900)),
        limitations=['No statistical noninferiority or universal tolerance.',
            'Four-slot correctness is not four-client HTTP performance acceptance.',
            'Representative finite arithmetic and MTP acceptance cases, not exhaustive inputs.',
            'No long soak, new SDK, real LAN deployment or managed/task migration.',
            'Old managed/task packages remain pinned; no client WIP or whole research merge.'],
        decision_tool_sha256=a.sha(Path(__file__)))
    a.require(sum(seconds.values()) <= 9900, 'overall bounded stage time')
    a.save(OUT / 'decision.json', decision)
    a.save(OUT / 'retained-state-cache-files.json', retained)
    raw.update([OUT / 'decision.json', OUT / 'retained-state-cache-files.json', Path(__file__).resolve()])
    inputs.add(Path(__file__).resolve())
    input_receipt = archive(OUT / 'inputs.tar.gz', inputs, OUT / 'inputs-index.json')
    a.save(OUT / 'inputs-receipt.json', input_receipt)
    raw.add(OUT / 'inputs-receipt.json')
    raw_receipt = archive(OUT / 'raw-records.tar.gz', raw, OUT / 'raw-index.json')
    raw_receipt['input_archive'] = input_receipt
    raw_receipt['retained_large_files'] = len(retained)
    raw_receipt['retained_index_sha256'] = a.sha(OUT / 'retained-state-cache-files.json')
    a.save(OUT / 'archive-receipt.json', raw_receipt)
    print('PASS bounded engineering decision, verified inputs/raw archives, retained full state')


if __name__ == '__main__':
    main()
