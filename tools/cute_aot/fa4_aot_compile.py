"""Step 1b: AOT-compile the FA4 generic SM100/SM110 forward kernel (hd256).

Why the *generic* kernel (FlashAttentionForwardSm100), not the dedicated
hd256 2-CTA kernel (BlackwellFusedMultiHeadAttentionForward)?

  * The dedicated hd256 kernel asserts ``blocksparse_tensors is None`` and
    requires paged-KV page_size == tile_n == 128. Our QSA is block-sparse
    with page_size 16, so it cannot use the dedicated kernel.
  * The generic kernel supports hd256 (tuning table has 256 entries),
    block sparsity, ``paged_kv_non_tma`` (page 16), GQA, and causal.

This script compiles the *minimal rectangular* configuration that matches our
model (head_dim 256, GQA 24:2, causal, bf16) to prove the FA4 -> AOT ->
.h/.o -> C++ driver chain works end to end. Paged KV / varlen / block-sparse
tensors are added in Step 2/3 once the base chain is green.

Build-time only: this runs in the cute-venv (Python 3.12 + torch cu132).
The emitted .h/.o are consumed by a pure C++ driver (no Python at runtime).

Usage:
  .q4t-work/cute-venv/bin/python tools/cute_aot/fa4_aot_compile.py <out_dir>
"""
import faulthandler
import os
import sys

faulthandler.enable()

import torch

_FA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                        "..", "..", "reference",
                                        "flash-attention"))
sys.path.insert(0, _FA_ROOT)

# The top-level flash_attn/__init__.py imports the FA2 C extension
# (flash_attn_2_cuda), which is not built here. We only need the pure-Python
# flash_attn.cute.* subpackage, so pre-seed a stub `flash_attn` package whose
# __path__ points at the real dir; `import flash_attn.cute.X` then resolves
# without executing the top-level __init__.py.
import types
_pkg = types.ModuleType("flash_attn")
_pkg.__path__ = [os.path.join(_FA_ROOT, "flash_attn")]
_pkg.__package__ = "flash_attn"
sys.modules["flash_attn"] = _pkg

import cuda.bindings.driver as cuda
import cutlass
import cutlass.cute as cute
from cutlass import Float32, Int32
from cutlass.cute.runtime import make_fake_stream

from flash_attn.cute.flash_fwd_sm100 import FlashAttentionForwardSm100
from flash_attn.cute.cute_dsl_utils import to_cute_tensor
from flash_attn.cute.utils import AuxData

# Our model: head_dim 256, 24 q-heads, 2 kv-heads (GQA 12:1), causal, bf16.
HEAD_DIM = 256
NUM_HEADS_Q = 24
NUM_HEADS_KV = 2
QHEAD_PER_KVHEAD = NUM_HEADS_Q // NUM_HEADS_KV  # 12
DTYPE = torch.bfloat16


def build_rect_tensors(batch, seqlen_q, seqlen_k, device="cuda"):
    """Rectangular (non-varlen, non-paged) Q/K/V/O/LSE matching FA4 layout.

    mQ/mO: (b, s_q, h_q, d)   mK/mV: (b, s_k, h_kv, d)
    mLSE:  (b, h_q, s_q) fp32
    """
    q = torch.randn(batch, seqlen_q, NUM_HEADS_Q, HEAD_DIM,
                    device=device, dtype=DTYPE)
    k = torch.randn(batch, seqlen_k, NUM_HEADS_KV, HEAD_DIM,
                    device=device, dtype=DTYPE)
    v = torch.randn(batch, seqlen_k, NUM_HEADS_KV, HEAD_DIM,
                    device=device, dtype=DTYPE)
    o = torch.empty(batch, seqlen_q, NUM_HEADS_Q, HEAD_DIM,
                    device=device, dtype=DTYPE)
    lse = torch.empty(batch, NUM_HEADS_Q, seqlen_q,
                      device=device, dtype=torch.float32)
    return q, k, v, o, lse


def torch_reference(q, k, v, causal=True):
    """Naive causal attention reference. Inputs/outputs are (b, s, h, d)."""
    scale = HEAD_DIM ** -0.5
    # (b, s, h, d) -> (b, h, s, d)
    qf = q.float().permute(0, 2, 1, 3)
    kf = k.float().permute(0, 2, 1, 3).repeat_interleave(
        QHEAD_PER_KVHEAD, dim=1)
    vf = v.float().permute(0, 2, 1, 3).repeat_interleave(
        QHEAD_PER_KVHEAD, dim=1)
    sq = qf.shape[2]
    sk = kf.shape[2]
    # scores: (b, hq, sq, sk)
    scores = torch.matmul(qf, kf.transpose(-1, -2)) * scale
    if causal:
        i = torch.arange(sq, device=q.device)
        j = torch.arange(sk, device=q.device)
        mask = j <= (i + (sk - sq))[:, None]  # bottom-right aligned causal
        scores = scores.masked_fill(~mask[None, None], float("-inf"))
    p = torch.softmax(scores, dim=-1)
    o = torch.matmul(p, vf)  # (b, hq, sq, d)
    return o.permute(0, 2, 1, 3).to(DTYPE)  # back to (b, s, h, d)


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out_dir, exist_ok=True)

    # Minimal rectangular config: 1 batch, 128x128 (tile-aligned), causal.
    batch, seqlen_q, seqlen_k = 1, 128, 128
    q, k, v, o, lse = build_rect_tensors(batch, seqlen_q, seqlen_k)

    # --- Numerical reference (torch, fp32) before kernel overwrites o ---
    ref = torch_reference(q, k, v, causal=True)

    # --- cute tensors (TVM-FFI, dynamic leading dim) ---
    q_t = to_cute_tensor(q)
    k_t = to_cute_tensor(k)
    v_t = to_cute_tensor(v)
    o_t = to_cute_tensor(o)
    lse_t = to_cute_tensor(lse, assumed_align=4)

    softmax_scale = Float32(HEAD_DIM ** -0.5)

    fa_fwd = FlashAttentionForwardSm100(
        head_dim=HEAD_DIM,
        head_dim_v=HEAD_DIM,
        qhead_per_kvhead=QHEAD_PER_KVHEAD,
        is_causal=True,
        is_local=False,
        is_split_kv=False,
        pack_gqa=False,
        m_block_size=128,
        n_block_size=128,
        # hd256 in the *generic* kernel is 1-CTA only (the 2-CTA path requires
        # head_dim in [128,192]); q_stage=1 keeps tmem_total = 512 <= 512 cols.
        q_stage=1,
        is_static_persistent=True,
        use_2cta_instrs=False,
        use_clc_scheduler=False,
    )

    # The FA4 __call__ has a `aux_data: AuxData` NamedTuple param that the C
    # header generator cannot represent (it only supports Pointer / Tensor /
    # Numeric / CUstream, and skips None). The kernel body reads
    # `aux_data.tensors` unconditionally, so we cannot pass None. Instead we
    # wrap the call in our own @cute.jit function whose signature contains only
    # C-exportable params; AuxData is constructed *inside* as a compile-time
    # constant. cute.compile exports the *top-level* (wrapper) signature, so
    # the emitted C wrapper has no AuxData argument.
    @cute.jit
    def fa4_fwd_wrapper(mQ: cute.Tensor, mK: cute.Tensor, mV: cute.Tensor,
                        mO: cute.Tensor, mLSE: cute.Tensor,
                        softmax_scale: Float32,
                        max_seqlen_q: Int32, stream: cuda.CUstream):
        fa_fwd(
            mQ, mK, mV, mO, mLSE,
            softmax_scale,
            None, None, None, None,   # cu_seqlens_q/k, seqused_q/k
            None,                     # page_table
            None, None,               # window_size_left/right
            None,                     # learnable_sink
            None,                     # descale_tensors
            None,                     # blocksparse_tensors
            AuxData(None, None),      # aux_data (compile-time constant)
            None, None, None, None,   # num_splits_dynamic, tile_count_sem,
            #                        # virtual_batch_idx, num_nheads_in_l2
            None, None, None,         # cu_total_m_blocks, cu_total_splits,
            #                        # blocks_to_batch_idx
            max_seqlen_q,
            stream,
        )

    stream = make_fake_stream(use_tvm_ffi_env_stream=True)

    print(">>> compiling wrapper...", flush=True)
    compiled = cute.compile(
        fa4_fwd_wrapper,
        q_t, k_t, v_t, o_t, lse_t,
        softmax_scale,
        Int32(seqlen_q),          # max_seqlen_q
        stream,
        options="--gpu-arch sm_110a",
    )
    print(">>> compile done, exporting to C...", flush=True)
    compiled.export_to_c(out_dir, "fa4_fwd_hd256")
    print("exported:", os.path.join(out_dir, "fa4_fwd_hd256.h"),
          os.path.join(out_dir, "fa4_fwd_hd256.o"))

    # --- Run the compiled kernel once (JIT path) and check vs reference ---
    # The compiled callable expects cute tensors (same objects used at compile
    # time), not raw torch tensors.
    print(">>> running compiled kernel (JIT path)...", flush=True)
    compiled(
        q_t, k_t, v_t, o_t, lse_t,
        softmax_scale,
        Int32(seqlen_q),
        stream,
    )
    print(">>> call returned, synchronizing...", flush=True)
    torch.cuda.synchronize()
    print(">>> sync done", flush=True)
    out = o.float()
    r = ref.float()
    l2_rel = (out - r).norm() / r.norm().clamp_min(1e-12)
    argmax_match = (out.argmax(-1) == r.argmax(-1)).float().mean().item()
    print(f"[jit-check] l2_rel={l2_rel.item():.6f} "
          f"argmax_match={argmax_match:.4f}")
    if l2_rel.item() > 0.05:
        print("[jit-check] WARNING: l2_rel above 0.05 — inspect before AOT use")
        sys.exit(2)


if __name__ == "__main__":
    main()
