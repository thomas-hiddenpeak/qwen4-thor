#!/usr/bin/env python3
"""L2 noise-fidelity comparison: C++ NVFP4 vs transformers FP32 reference.

Same-input comparison (both run on the identical prompt). For each position t:
  - l2_rel[t] = ||L_cpp[t]-L_ref[t]|| / ||L_ref[t]||   (NVFP4 noise band)
  - argmax match (C++ vs reference)
  - reference gap g_t = L_ref[top1] - L_ref[top2]
  - noise delta_t = (L_cpp[top2]-L_cpp[top1]) - (L_ref[top2]-L_ref[top1])

Threshold tau = 3 * std(delta) over all positions.

The C++ engine is NVFP4 W4A4; the reference is dequantized FP32. The
difference IS the quantization noise, so the PASS criteria ask "noise only,
or systematic bug?":
  [A] every CONFIDENT position (reference gap g_t > tau) matches argmax;
  [B] every mismatch is a near-tie (g_t <= tau);
  [C] l2_rel in the W4A4 band (mean ~0.2, max < 0.75).

Exit code: 0 if A and B and C all PASS, else 1. (The original
.q4t-work/l2_compare.py only printed a verdict; this copy is the
self-contained, exit-code-bearing version used by verify_logits.py.)

Usage: python3 compare_logits.py <cpp_prefill.bin> <ref_logits.npy>
"""
import sys

import numpy as np


def main():
  cpp_path, ref_path = sys.argv[1], sys.argv[2]
  vocab = 248320
  cpp = np.fromfile(cpp_path, dtype=np.float32)
  cpp = cpp.reshape(-1, vocab)
  ref = np.load(ref_path).astype(np.float32)
  T = min(cpp.shape[0], ref.shape[0])
  cpp, ref = cpp[:T], ref[:T]
  print(f"C++ {cpp.shape}, ref {ref.shape}, comparing T={T}")

  # Per-position l2_rel.
  l2_rel = np.zeros(T)
  for t in range(T):
    l2_rel[t] = np.linalg.norm(cpp[t] - ref[t]) / (
        np.linalg.norm(ref[t]) + 1e-9
    )

  # Per-position top-2 + gap + noise delta + argmax match.
  cpp_am = cpp.argmax(axis=1)
  ref_am = ref.argmax(axis=1)
  match = cpp_am == ref_am
  gaps = np.zeros(T)
  deltas = np.zeros(T)
  for t in range(T):
    r_sorted = np.argsort(ref[t])[::-1]
    a, b = int(r_sorted[0]), int(r_sorted[1])
    gaps[t] = ref[t, a] - ref[t, b]
    deltas[t] = (cpp[t, b] - cpp[t, a]) - (ref[t, b] - ref[t, a])

  n_match = int(match.sum())
  mism = np.where(~match)[0]
  print(f"\n=== argmax match: {n_match}/{T} ({100*n_match/T:.1f}%) ===")
  print(f"l2_rel: mean {l2_rel.mean():.4f}  max {l2_rel.max():.4f}  "
        f"p99 {np.percentile(l2_rel, 99):.4f}")

  sigma_delta = float(deltas.std())
  tau = 3.0 * sigma_delta
  print(f"\nnoise delta: std(sigma)={sigma_delta:.4f}  -> tau=3sigma={tau:.4f}")

  confident = gaps > tau
  n_conf = int(confident.sum())
  if n_conf == 0:
    critA_ok = False
    print("\n[A] no confident positions (gap>tau) - cannot validate greedy")
  else:
    conf_match = match[confident]
    critA_ok = bool(conf_match.all())
    bad = confident & ~match
    print(f"\n[A] CONFIDENT-position argmax match (primary): "
          f"{int(conf_match.sum())}/{n_conf} "
          f"{'PASS' if critA_ok else 'FAIL'}")
    if bad.sum() > 0:
      print(f"    confident MISMATCHES at {np.where(bad)[0].tolist()}")

  if len(mism) == 0:
    critB_ok = True
    print(f"[B] no mismatches -> all argmax agree: PASS")
  else:
    m_gaps = gaps[mism]
    worst = int(np.argmax(m_gaps))
    critB_ok = bool(m_gaps.max() <= tau)
    print(f"[B] {len(mism)} mismatches; reference gaps among them: "
          f"max {m_gaps.max():.4f} (tau={tau:.4f})")
    print(f"    mismatch positions (first 20): {list(mism[:20])}")
    print(f"    worst mismatch pos {mism[worst]}: gap={gaps[mism[worst]]:.4f} "
          f"delta={deltas[mism[worst]]:.4f} "
          f"(flip needs delta>gap: {deltas[mism[worst]] > m_gaps[worst]})")
    print(f"    -> every mismatch is a near-tie (gap<=tau): "
          f"{'PASS' if critB_ok else 'FAIL'}")

  # The l2_rel band is layer-count dependent (noise accumulates per layer):
  # 4-layer smoke ~0.05, 16-layer ~0.21, full 48-layer ~0.14. The [0.10, 0.35]
  # band is calibrated for the full model; a short-layer smoke test may sit
  # below it (then [C] is informational, [A]/[B] are the real signal).
  band_ok = bool(0.10 <= l2_rel.mean() <= 0.35 and l2_rel.max() < 0.75)
  print(f"[C] l2_rel W4A4 band: mean {l2_rel.mean():.4f} "
        f"(48-layer ~0.14), max {l2_rel.max():.4f} (<0.75): "
        f"{'PASS' if band_ok else 'FAIL'}")

  # Context: near-tie fraction explains why raw argmax agreement is low.
  near_tie_frac = float((gaps < tau).mean())
  print(f"\ncontext: near-tie fraction (gap<tau) = {100*near_tie_frac:.1f}% "
        f"of {T} positions")
  print(f"        raw argmax match = {n_match}/{T} "
        f"({100*n_match/T:.1f}%) - low because {100*near_tie_frac:.1f}% "
        f"of positions are reference near-ties where the choice is within "
        f"noise; a correct engine matches ~50% of those by chance plus all "
        f"confident positions.")

  overall = critA_ok and critB_ok and band_ok
  print(f"\n=== OVERALL: {'PASS' if overall else 'REVIEW'} ===")
  return 0 if overall else 1


if __name__ == "__main__":
  sys.exit(main())
