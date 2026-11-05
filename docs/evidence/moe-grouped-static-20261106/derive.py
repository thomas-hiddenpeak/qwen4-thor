"""Read existing sources/records and print static review arithmetic; no tests.

Run from this worktree with Python -B. This script never imports CUDA, invokes
an executable under review, sends HTTP, queries heuristics, or applies its patch.
"""

import difflib
import hashlib
import json
import re
from pathlib import Path


ROOT = Path('/home/rm01/models/dev/qwen4-thor')
WORK = Path(__file__).resolve().parents[3]


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


manifest_path = ROOT / (
    '.q4t-work/serve-client-governance-20261105/completion/package/'
    'candidate/runtime-source.json')
manifest = json.loads(manifest_path.read_text())
source_path = ROOT / 'src/quant/moe_decode.cu'
source = source_path.read_text()
profile_path = WORK / (
    'docs/evidence/default-verify-profile-20261106/analysis.json')
profile = json.loads(profile_path.read_text())
old_dir = ROOT / '.q4t-work/moe-share-diag-174406'
old_log = old_dir / 'server.log'
matches = re.findall(
    r'\[moe-share-diag\] layers=(\d+) rows=(\d+) unique=(\d+)',
    old_log.read_text())
layers, rows, unique = map(int, matches[-1])

cases = {}
prefill = {}
for key, case in profile['cases'].items():
    if case['mode'] != 'fine':
        continue
    head, context = case['head'], case['case']
    coarse = next(c for c in profile['cases'].values()
                  if c['mode'] == 'coarse' and c['head'] == head
                  and c['case'] == context)
    normal = next(c for c in profile['cases'].values()
                  if c['mode'] == 'normal' and c['head'] == head
                  and c['case'] == context)
    round_ms = sum(coarse['timings']['mtp_host']['mean_ms'].values())
    moe = case['timings']['moe-subtiming']['4']['mean_ms']
    prep = moe['prep_gu'] + moe['prep_dn']
    gemm = moe['gemm_gu'] + moe['gemm_dn']
    http = normal['http']
    cases[key] = {
        'coarse_round_ms': round_ms,
        'fine_moe_mean_ms': moe,
        'fine_prepare_gu_dn_ms': prep,
        'fine_gemm_gu_dn_ms': gemm,
        'round_saving_ms_for_3pct_throughput': round_ms * (1 - 1 / 1.03),
        'gemm_fraction_to_remove_for_3pct':
            round_ms * (1 - 1 / 1.03) / gemm,
        'ideal_round_gain_if_all_prepare_removed_pct':
            100 * (round_ms / (round_ms - prep) - 1),
        'normal_http_ttft_fraction': http['ttft'] / http['latency'],
        'normal_http_time_drop_if_decode_3pct_faster_pct':
            100 * (http['latency'] - http['ttft']) * (1 - 1 / 1.03)
            / http['latency'],
    }
    if context == 'context-45056':
        chunks = [(int(t), b) for t, b in case['timings']['sub-timing'].items()
                  if int(t) > 8]
        prefill[key] = {
            'rows': sum(t * b['count'] for t, b in chunks),
            'chunks': [{'tokens': t, 'count': b['count']} for t, b in chunks],
            'fine_sum_ms': {
                name: sum(b['sum_ms'][name] for _, b in chunks)
                for name in ('total', 'lin_state', 'full_core', 'moe')},
            'source': case['source'],
        }

needle = 'MoEDecodeGroupBatchKernel<<<R, 128, 0, stream>>>'
proposal = source.replace(needle, 'MoEDecodeGroupBatchKernel<<<1, 128, 0, stream>>>')
proposal = proposal.replace(
    'constexpr int kMaxRows = kMaxT * kSlots;  // 80\n',
    'constexpr int kMaxRows = kMaxT * kSlots;  // 80\n'
    'static_assert(kMaxRows <= 128, "Grouped batch needs one 128-thread block");\n')
patch = ''.join(difflib.unified_diff(
    source.splitlines(keepends=True), proposal.splitlines(keepends=True),
    fromfile='a/src/quant/moe_decode.cu', tofile='b/src/quant/moe_decode.cu'))

paths = [source_path, WORK / 'src/quant/moe_decode.cu',
         ROOT / 'tests/moe_decode_grouped_test.cpp',
         ROOT / 'src/quant/moe_gemm.cu',
         ROOT / 'src/model/decoder_layer.cu',
         ROOT / 'include/q4t/quant/lt_cache.h',
         Path('/usr/local/cuda/include/cublasLt.h'),
         Path('/usr/local/cuda/include/cublas_api.h'),
         profile_path, manifest_path, old_log,
         old_dir / 'commit.txt', old_dir / 'binary.sha256',
         ROOT / 'build/q4t', ROOT / 'build/q4t.release.json']
for arm in ('02-coarse-full', '04-fine-full', '05-fine-subset'):
    paths.append(WORK / '.q4t-work/default-verify-profile-20261106/run'
                 / arm / 'server.log')
result = {
    'status': 'STATIC_REVIEW_COMPLETE_NOT_RUNTIME_ACCEPTANCE',
    'new_measurements': 0,
    'authorization': 'Static review only; no tests, builds, model or GPU use.',
    'inputs': [{'path': str(p), 'sha256': sha(p)} for p in paths],
    'accepted_runtime_source_count': len(manifest),
    'main_runtime_source_mismatches': [p for p, s in manifest.items()
                                       if sha(ROOT / p) != s],
    'research_runtime_differences_from_accepted': [
        p for p, s in manifest.items() if sha(WORK / p) != s],
    'fixed_group_shape': {
        'T': 4, 'top_k': 10, 'descriptor_count': 40,
        'sum_active_rows': 40, 'mean_over_all_descriptors': 1,
        'active_expert_count_runtime_known_to_host': False,
        'current_M_e_distribution': 'NOT_CAPTURED_IN_REUSED_CURRENT_PROFILE',
    },
    'historical_diagnostic_not_current_distribution': {
        'commit': (old_dir / 'commit.txt').read_text().strip(),
        'elf_sha256': (old_dir / 'binary.sha256').read_text().strip(),
        'last_cumulative': {'layers': layers, 'rows': rows, 'unique': unique},
        'unique_fraction': unique / rows,
        'mean_rows_per_active_expert': rows / unique,
        'limitation': 'Cumulative totals only, no M_e histogram; older runtime.',
    },
    'cases': cases,
    'prefill_existing_record_reduction': prefill,
    'batch_grid': {
        'source_launch_sites': source.count(needle),
        'T4_blocks_per_two_launches_before': 80,
        'T4_blocks_per_two_launches_proposed': 2,
        'launch_count_change': 0,
        'group_count_change': 0,
        'supports_T': [2, 3, 4, 5, 6, 7, 8], 'max_R': 80,
        'proposed_block_threads': 128,
        'patch_applied': False, 'patch': patch,
    },
    'decisions': {
        'change_average_hint_to_active_mean': 'NO_STATIC_JUSTIFICATION',
        'heuristic_search_or_algorithm_replacement': 'DEFER_NO_3PCT_EVIDENCE',
        'descriptor_grid': 'MINIMAL_UNAPPLIED_PROPOSAL_NOT_3PCT_CANDIDATE',
        'deploy_or_merge': 'NOT_ACCEPTED',
    },
    'limitations': [
        'Fine timings include observer effects and possible submission gaps.',
        'Combining fine/coarse for ideal savings is a screening estimate only.',
        'No measured occupancy, DRAM traffic, alternative algorithm or grid speedup.',
        'No new tests or cuBLAS heuristic/library calls were performed.',
        'The repeated-store correction has no independent runtime evidence yet.',
    ],
}
print(json.dumps(result, indent=2, ensure_ascii=False))
