#!/usr/bin/env python3
"""Roofline analysis of a Qwen4-Thor prefill: is compute or memory the bound?

Uses the real model dims + measured prefill wall time to compute, per major
component, the FLOPs, the bytes moved, the arithmetic intensity, and which side
of the roofline ridge it sits on. Answers "are the tensor cores fully used?".
"""

# ---- Model dims (docs/MODEL.md) ----
L = 48                 # layers
L_full = 12            # full_attention (QSA) layers
L_lin = 36             # linear_attention (GatedDeltaNet) layers
hs = 2560              # hidden_size
E = 512                # routed experts
k = 10                 # top-k
E_shared = 1
moe_is = 640           # moe_intermediate_size
# full_attn (QSA GQA)
nq, nkv, hd = 24, 2, 256
idx_budget = 2048      # sparse: each query attends ~this many keys
# linear_attn (GDN)
nkh, nv, kd, vd = 16, 48, 128, 128
# workload
T = 2560               # prefill tokens
t_prefill = 2.60       # measured wall time (s), q4t generate, 2560 tok @ 989 tok/s

# ---- Hardware (Jetson AGX Thor SM110a, Blackwell, measured/spec) ----
BW = 241e9             # LPDDR5x achieved bandwidth (B/s), measured copy kernel
# Blackwell tensor-core dense peak (TFLOPS). Ratio FP4:FP8:BF16 = 4:2:1.
PEAK_FP4 = 1035e12
PEAK_BF16 = 259e12

GB = 1e9
TF = 1e12

def gemm_flops(M, N, K):
    return 2.0 * M * N * K

# ============ FLOPs per component (whole prefill, T tokens) ============
# MoE routed: per token, k experts, each w13 [hs->2*moe_is] + w2 [moe_is->hs].
moe_routed_flops = T * L * k * (gemm_flops(1, 2*moe_is, hs) + gemm_flops(1, hs, moe_is))
moe_shared_flops = T * L * E_shared * (gemm_flops(1, 2*moe_is, hs) + gemm_flops(1, hs, moe_is))
router_flops = T * L * gemm_flops(1, E, hs)

# full_attn projections + sparse attention compute (12 layers).
attn_proj_flops = T * L_full * (
    gemm_flops(1, nq*hd, hs) +   # q
    gemm_flops(1, nkv*hd, hs) +  # k
    gemm_flops(1, nkv*hd, hs) +  # v
    gemm_flops(1, hs, nq*hd))    # o
# sparse attention: each query attends idx_budget keys. QK^T + softmax@V.
attn_core_flops = T * L_full * nq * idx_budget * hd * 2 * 2  # QK (x2) + AV (x2)

# linear_attn projections (36 layers): in_proj (qkvz+ab) + out_proj.
in_proj_out = nkh*kd*2 + nv*vd + nv*vd  # q,k (nkh) ; v,z (nv)  ~= 4096+6144+6144
gdn_proj_flops = T * L_lin * (gemm_flops(1, in_proj_out, hs) + gemm_flops(1, hs, nv*vd))
# GDN recurrence: per token per (kh, vh-group): outer(k,v) + state*q. ~2*kd*vd.
gdn_core_flops = T * L_lin * nv * (2*kd*vd + 2*kd*vd)

total_flops = (moe_routed_flops + moe_shared_flops + router_flops +
               attn_proj_flops + attn_core_flops + gdn_proj_flops + gdn_core_flops)

# ============ Bytes moved (whole prefill) ============
# MoE weights (fp4 = 0.5 B/elem) + scales(~ negligible). All E experts active
# (T*k=25600 assignments >> E=512, so ~every expert non-empty each layer).
w13_bytes = (2*moe_is) * hs * 0.5
w2_bytes = hs * moe_is * 0.5
moe_w_bytes = L * (E + E_shared) * (w13_bytes + w2_bytes)
router_w_bytes = L * E * hs * 2  # bf16 router
# dense projection weights (bf16), loaded once, applied to all T (reused).
attn_proj_w = L_full * (nq*hd*hs + nkv*hd*hs*2 + hs*nq*hd) * 2
gdn_proj_w = L_lin * (in_proj_out*hs + hs*nv*vd) * 2
# KV cache traffic for sparse attn: the KV cache is small (fits L2 across a
# layer), so queries re-reading selected blocks mostly hit L2. DRAM ~= the KV
# written once + a few-fold re-read. Model as KV_size * reread.
kv_size = L_full * T * nkv * hd * 2 * 2  # K+V, bf16
kv_bytes = kv_size * 4  # ~4x effective DRAM re-read (rest served by L2)
# GDN state stays in shared memory across the sequence (66KB, occupancy-capped);
# it is NOT streamed to DRAM per token. DRAM = load/store state once per
# (layer, head) + the per-token q/k/v/z activation I/O (counted in act_bytes).
gdn_state_bytes = L_lin * nv * kd * vd * 4 * 2
# activations (hidden states streamed through layers), rough.
act_bytes = L * T * hs * 2 * 6  # ~6 read/write passes per layer

total_bytes = (moe_w_bytes + router_w_bytes + attn_proj_w + gdn_proj_w +
               kv_bytes + gdn_state_bytes + act_bytes)

# ============ Report ============
ridge_fp4 = PEAK_FP4 / BW
ridge_bf16 = PEAK_BF16 / BW
print(f"=== Qwen4-Thor prefill roofline (T={T} tok, {t_prefill}s) ===")
print(f"Thor: BW={BW/GB:.0f} GB/s | peak FP4={PEAK_FP4/TF:.0f} TF (ridge {ridge_fp4:.0f} F/B)"
      f" | peak BF16={PEAK_BF16/TF:.0f} TF (ridge {ridge_bf16:.0f} F/B)\n")

print(f"{'component':<22}{'GFLOP':>9}{'GB':>8}{'FLOP/B':>9}  bound")
def row(name, fl, by, prec='fp4'):
    ai = fl/by if by else 0
    ridge = ridge_fp4 if prec=='fp4' else ridge_bf16
    b = 'COMPUTE' if ai > ridge else 'memory'
    print(f"{name:<22}{fl/GB:>9.0f}{by/GB:>8.1f}{ai:>9.0f}  {b}")
    return fl, by

row("MoE routed", moe_routed_flops, moe_w_bytes*(E/(E+E_shared)), 'fp4')
row("MoE shared", moe_shared_flops, moe_w_bytes*(E_shared/(E+E_shared)), 'fp4')
row("attn proj", attn_proj_flops, attn_proj_w, 'fp4')
row("attn core (sparse)", attn_core_flops, kv_bytes, 'bf16')
row("GDN proj", gdn_proj_flops, gdn_proj_w, 'fp4')
row("GDN recurrence", gdn_core_flops, gdn_state_bytes, 'bf16')

print(f"\n{'TOTAL':<22}{total_flops/GB:>9.0f}{total_bytes/GB:>8.1f}"
      f"{total_flops/total_bytes:>9.0f}")

ach_tflops = total_flops / t_prefill
print(f"\nAchieved compute : {ach_tflops/TF:6.1f} TFLOPS "
      f"= {100*ach_tflops/PEAK_FP4:4.1f}% FP4 peak / {100*ach_tflops/PEAK_BF16:4.1f}% BF16 peak")
print(f"  -> tensor cores idle ~{100-100*ach_tflops/PEAK_BF16:.0f}% of the time.")
print(f"\nTime floor if compute-bound (FP4 peak): {total_flops/PEAK_FP4:.2f}s")
print(f"Measured prefill wall time            : {t_prefill:.2f}s")
print(f"  -> {t_prefill/(total_flops/PEAK_FP4):.0f}x the compute floor: the wall time is")
print(f"     memory movement + latency stalls, NOT arithmetic.")
print(f"\nNote: DRAM total {total_bytes/GB:.0f} GB is a LOWER bound (GDN state lives in")
print(f"shared mem; sparse-KV re-reads partly hit L2). True DRAM is higher, but even")
print(f"at 100% bandwidth the memory floor ({total_bytes/BW:.2f}s+) dwarfs compute ({total_flops/PEAK_FP4:.2f}s).")

# Empirical scaling (q4t generate, current build): tok/s is flat-to-declining
# with prefill size, so throughput is NOT limited by amortizable fixed weight
# loads (those would make bigger prefills faster/tok). It is limited by
# per-token memory/latency work that grows with T (sparse-attn KV gather).
#   T=1280 -> 1012.7 tok/s | T=2560 -> 989.4 | T=5120 -> 950.7
print("\nEmpirical scaling: T=1280->1012.7 tok/s | 2560->989.4 | 5120->950.7")
print("  (flat/declining => per-token memory-bound, not weight-amortization-bound)")
