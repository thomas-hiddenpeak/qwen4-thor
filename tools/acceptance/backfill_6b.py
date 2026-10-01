#!/usr/bin/env python3
"""Generate the §6b markdown block for MOE_RESIDENCY_ACCEPTANCE from
final-acceptance B outputs (compare report, memory gate, c3 evidence).
Usage: backfill_6b.py <B>
Prints markdown to stdout; review before pasting into the report.
"""
import json
import re
import sys
from pathlib import Path

W = Path(__file__).resolve().parent
B = sys.argv[1] if len(sys.argv) > 1 else '12288'

def read(p):
    return Path(p).read_text(errors='replace') if Path(p).is_file() else None

# --- compare report: per-tier table rows -----------------------------------
rep = read(W / f'compare-report-final-{B}.txt')
rows = []
runs = {}
target = []
memlines = []
gate_line = []
if rep:
    for m in re.finditer(
            r'^\s*(\d+)\s+\|\s+([\d.]+|nan)\s+\|\s+([\d.]+|nan)\s+\|\s+([\d.]+|nan)\s+\|\s+(\w+)\s+\|\s+([\d.]+)\s+\|\s+([\d.]+)\s+\|\s+([\d.]+)\s+\|\s+([\d.]+)\s+\|\s+(\w+)',
            rep, re.M):
        tier, bh, ch, ratio, gate, btt, ctt, bl, cl, exact = m.groups()
        rows.append((tier, bh, ch, ratio, gate, btt, ctt, bl, cl, exact))
    # per-run detail lines (first vs subsequent)
    for m in re.finditer(r'^\s+run(\d): base ttft=([\d.]+)s dec=([\d.]+) \| cand ttft=([\d.]+)s dec=([\d.]+)', rep, re.M):
        i, btt, bd, ctt, cd = m.groups()
        runs.setdefault(i, []).append((btt, bd, ctt, cd))
    # target tier contract
    target = [l.strip() for l in rep.splitlines()
              if 'target tier token contract' in l or re.match(r'^(base|cand): in/out/finish', l.strip())]
    # memory peaks from report
    memlines = [l.strip() for l in rep.splitlines() if re.match(r'^(acc-base-c0|acc-final-\d+): \d+', l.strip())]
    gate_line = [l.strip() for l in rep.splitlines() if l.strip() in ('ALL PASS', 'FAIL (see tiers above)') or l.startswith('INCOMPLETE')]

# --- memory gate json -------------------------------------------------------
mg = W / f'memory-gate-final-{B}.json'
mgd = json.loads(mg.read_text()) if mg.is_file() else None

# --- c3 evidence ------------------------------------------------------------
def c3ev(tag):
    p = W / f'c3-{tag}' / 'evidence.json'
    return json.loads(p.read_text()) if p.is_file() else None
evb, evc = c3ev('acc-base-c0'), c3ev(f'acc-final-{B}')

out = []
out.append('## 6b. 最终候选验收（C1+C2+C3+C4+C5+C6，final-acceptance）')
out.append('')
out.append(f'- 最终候选 B：{B}（C=256 每层命中 top-n，用户 2026-10-01 07:25 授权超支）')
out.append('- 二进制：ed68cd3d148e5fef…（C1+C2+C3+C4+C5+C6）')
if evb and evc:
    out.append(f"- C3 证据：基线 pc_delta={evb.get('page_cache_delta_matrix_gb', evb.get('page_cache_delta_gb'))} GB / "
               f"候选 pc_delta={evc.get('page_cache_delta_matrix_gb', evc.get('page_cache_delta_gb'))} GB；"
               f"候选 warm pread_avg={evc.get('warm_pread_avg_ms')} ms（冷 {evc.get('pread_avg_ms')} ms）")
if mgd:
    out.append(f"- 内存门：候选 rss+gpu 峰值 {mgd['candidate_rss_gpu_peak']/1e9:.2f} GB + "
               f"模型页缓存增量 {mgd['candidate_page_cache_delta']/1e9:.2f} GB = "
               f"{mgd['candidate_total']/1e9:.2f} GB vs 54 GB → **{mgd['gate']}**"
               + ('（用户授权超支）' if mgd.get('user_approved_overrun') else ''))
out.append('- 六档 decode（每档 3 次，调和均值；首/后续分列见下）：')
out.append('')
out.append('| 档 | 基线 tps | 候选 tps | 比值 | ≥50%? | 基线 TTFT | 候选 TTFT | 基线耗时 | 候选耗时 | bitexact |')
out.append('|---|---:|---:|---:|:---:|---:|---:|---:|---:|:---:|')
for r in rows:
    tier, bh, ch, ratio, gate, btt, ctt, bl, cl, exact = r
    label = f'{tier}（总上下文 262144）' if tier == '261887' else tier
    out.append(f'| {label} | {bh} | {ch} | {ratio} | {gate} | {btt} | {ctt} | {bl} | {cl} | {exact} |')
if runs:
    out.append('')
    out.append('首/后续请求分列（run1=首请求，run2/3=后续）：')
    out.append('')
    out.append('| run | 基线 ttft/dec | 候选 ttft/dec |')
    out.append('|---|---|---|')
    for i in sorted(runs):
        for (btt, bd, ctt, cd) in runs[i]:
            out.append(f'| run{i} | {btt}s / {bd} | {ctt}s / {cd} |')
if target:
    out.append('')
    out.append('目标档 token 合同：' + '；'.join(target))
if gate_line:
    out.append('')
    out.append(f'GATE 判定：{gate_line[0]}')
print('\n'.join(out))
