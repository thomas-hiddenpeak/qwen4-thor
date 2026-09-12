"""4-layer qwen4_exp reference forward (transformers classes + real weights).

Generalization of ref2_logits.py: N_LAYERS layers (default 4 = layer 0/1
linear_attention + layer 2 linear_attention + layer 3 full_attention), so the
first full_attention (QSA) layer is exercised inside the real layer loop — the
key gap left by the 2-layer reference (which was all-linear).

Loads real checkpoint weights (BF16 direct, NVFP4 routed experts dequantized
in numpy with a vectorized unswizzle), replaces the 51 GB PLE nn.Embedding
with an on-demand sidecar gather, runs the forward for the fixed token
sequence, and dumps logits [T, vocab] float32 for comparison with the C++
engine (model_forward_dump_logits with Q4T_MODEL_LAYERS=4).

Usage: python ref4_logits.py [out_prefix]
"""
import json
import math
import os
import struct
import sys

import numpy as np
import torch
import torch.nn.functional as F

from transformers.models.qwen4_exp.configuration_qwen4_exp import (
    Qwen4ExpTextConfig,
)
from transformers.models.qwen4_exp import modeling_qwen4_exp as _M
from transformers.models.qwen4_exp.modeling_qwen4_exp import (
    Qwen4ExpTextModel,
)

# ---------------------------------------------------------------------------
# Skip the ~100 GB PLE nn.Embedding allocation (forward is replaced by the
# sidecar gather). Re-implement __init__ with a tiny dummy embedding.
# ---------------------------------------------------------------------------
def _patched_ng_init(self, config, embedding_dim, layer_idx, ple_layer_index=0):
    import torch
    import torch.nn as nn

    super(_M.Qwen4ExpTextNGramEmbedding, self).__init__()
    self.layer_idx = layer_idx
    self.ngram_size = config.ngram_size
    self.context_len = self.ngram_size - 1
    self.heads_per_ngram = config.heads_per_ngram
    self.ngram_heads = (self.ngram_size - 1) * self.heads_per_ngram
    self.ple_layer_index = ple_layer_index
    self.unigram_vocab_size = config.vocab_size
    self.ngram_vocab_size_base = config.ngram_vocab_size_base
    head_dim_per_ngram = embedding_dim // self.ngram_heads
    self.seed = config.seed
    eos = config.eos_token_id
    self.eos_token_id = eos[0] if isinstance(eos, list) else eos
    self.head_vocab_sizes = []
    self.head_offsets = []
    self.total_vocab_size = 0
    for head_idx in range(self.ngram_heads):
        global_head_idx = self.ple_layer_index * self.ngram_heads + head_idx
        size = _M._find_nth_prime_after(self.ngram_vocab_size_base - 1, global_head_idx + 1)
        self.head_vocab_sizes.append(size)
        self.head_offsets.append(self.total_vocab_size)
        self.total_vocab_size += size
    self.layer_multipliers = nn.Buffer(
        _M._build_layer_multipliers(
            self.unigram_vocab_size, self.ngram_size, self.ple_layer_index, self.seed
        )
    )
    self.ngram_heads_vocab_sizes = nn.Buffer(
        torch.tensor(self.head_vocab_sizes, dtype=torch.long)
    )
    self.ngram_heads_offsets = nn.Buffer(
        torch.tensor(self.head_offsets, dtype=torch.long)
    )
    # Tiny dummy embedding (forward is replaced by the sidecar gather).
    self.ngram_embedding = nn.Embedding(8, head_dim_per_ngram)


_M.Qwen4ExpTextNGramEmbedding.__init__ = _patched_ng_init

# ---------------------------------------------------------------------------
# Lazy MoE experts: the real checkpoint stores 512 routed experts per layer in
# NVFP4. Holding all N_LAYERS layers' dequantized bf16 experts resident (~5 GB
# per layer) OOMs at 16 layers (~80 GB). Instead, replace the experts params
# with a 1-expert placeholder and dequantize a single layer's experts on demand
# during forward, freeing them right after. Peak drops to ~15 GB.
# ---------------------------------------------------------------------------
def _patched_experts_init(self, config):
    import torch
    import torch.nn as nn

    super(_M.Qwen4ExpTextExperts, self).__init__()
    self.num_experts = config.num_experts
    self.hidden_dim = config.hidden_size
    self.intermediate_dim = config.moe_intermediate_size
    # 1-expert placeholder (forward is replaced; never indexed by real ids).
    self.gate_up_proj = nn.Parameter(
        torch.zeros(1, 2 * self.intermediate_dim, self.hidden_dim)
    )
    self.down_proj = nn.Parameter(
        torch.zeros(1, self.hidden_dim, self.intermediate_dim)
    )
    self.act_fn = _M.ACT2FN[config.hidden_act]
    self._layer_id = None  # set by build_model; used to locate the shard prefix
    self._dequant_cache = {}  # expert_idx -> (gu, dn) bf16 tensors, freed per layer


def _patched_experts_forward(self, hidden_states, top_k_index, top_k_weights):
    import torch
    import torch.nn.functional as F

    self._dequant_cache = {}  # release any previously loaded layer's experts
    final_hidden_states = torch.zeros_like(hidden_states)
    with torch.no_grad():
        expert_mask = F.one_hot(top_k_index, num_classes=self.num_experts)
        expert_mask = expert_mask.permute(2, 1, 0)
        expert_hit = torch.greater(
            expert_mask.sum(dim=(-1, -2)), 0
        ).nonzero()

    for expert_idx in expert_hit:
        expert_idx = int(expert_idx[0])
        if expert_idx == self.num_experts:
            continue
        if expert_idx not in self._dequant_cache:
            self._dequant_cache[expert_idx] = _dequant_one_expert(
                self._layer_id, expert_idx
            )
        gu, dn = self._dequant_cache[expert_idx]
        top_k_pos, token_idx = torch.where(expert_mask[expert_idx])
        current_state = hidden_states[token_idx]
        gate, up = F.linear(current_state, gu).chunk(2, dim=-1)
        current_hidden_states = self.act_fn(gate) * up
        current_hidden_states = F.linear(current_hidden_states, dn)
        current_hidden_states = current_hidden_states * top_k_weights[
            token_idx, top_k_pos, None
        ]
        final_hidden_states.index_add_(
            0, token_idx, current_hidden_states.to(final_hidden_states.dtype)
        )

    self._dequant_cache = {}  # free this layer's experts before the next layer
    return final_hidden_states


_M.Qwen4ExpTextExperts.__init__ = _patched_experts_init
_M.Qwen4ExpTextExperts.forward = _patched_experts_forward

MDIR = os.environ.get(
    "Q4T_MODEL_DIR",
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream")
SIDECAR = os.path.join(MDIR, "ple/qwen3.8-flash-next-ple-fp8.bin")
IDS = [846, 25, 1203, 321]  # must match the C++ model_forward_dump_logits test
N_LAYERS = int(os.environ.get("Q4T_REF_LAYERS", "4"))
ROW_BYTES = 160
TOTAL_ROWS = 320001536

# ---------------------------------------------------------------------------
# safetensors tensor reader (mmap)
# ---------------------------------------------------------------------------
_idx = json.load(open(os.path.join(MDIR, "model.safetensors.index.json")))
_wm = _idx["weight_map"]
_mmap_cache = {}


def _shard_header(shard):
    if shard in _mmap_cache:
        return _mmap_cache[shard]
    import mmap

    fp = os.path.join(MDIR, shard)
    f = open(fp, "rb")
    mm = mmap.mmap(f.fileno(), 0, mmap.MAP_PRIVATE)
    n = struct.unpack("<Q", mm[:8])[0]
    hdr = json.loads(mm[8 : 8 + n])
    # Keep `f` alive (the mmap needs the fd to stay open).
    _mmap_cache[shard] = (mm, 8 + n, hdr, f)
    return _mmap_cache[shard]


def read_tensor(name):
    shard = _wm[name]
    mm, base, hdr, _f = _shard_header(shard)
    e = hdr[name]
    lo, hi = e["data_offsets"]
    raw = np.frombuffer(mm, dtype=np.uint8, count=hi - lo, offset=base + lo)
    shape = tuple(e["shape"])
    if e["dtype"] == "BF16":
        a = np.frombuffer(raw, dtype=np.uint16).astype(np.uint32) << 16
        a = a.view(np.float32).copy()
    elif e["dtype"] == "F32":
        a = np.frombuffer(raw, dtype=np.float32).copy()
    elif e["dtype"] == "I64":
        a = np.frombuffer(raw, dtype=np.int64).copy()
    elif e["dtype"] in ("U8", "F8_E4M3"):
        a = np.frombuffer(raw, dtype=np.uint8).copy()
    else:
        raise ValueError(f"unsupported dtype {e['dtype']}")
    return a.reshape(shape) if shape else a


# ---------------------------------------------------------------------------
# NVFP4 dequant (mirrors include/q4t/quant/swizzle.h + dequant.cu)
#   W_real = e2m1(w) * e4m3(sf_swizzled) * weight_scale_2
# ---------------------------------------------------------------------------
_E2M1 = np.array(
    [0, 0.5, 1, 1.5, 2, 3, 4, 6, -0, -0.5, -1, -1.5, -2, -3, -4, -6],
    dtype=np.float32,
)


def _e4m3_decode(x):
    # Signed e4m3fn (matches CUDA __NV_E4M3 for the PLE sidecar, and the
    # unsigned UE4M3 for expert scales since their sign bit is always 0):
    #   sign(1) exp(4) man(3)
    #   exp=0: subnormal 0.5 * man/8
    #   exp=15, man=7 (0x7F / 0xFF): NaN -> 0.0 (guard, matches C++)
    #   0x78..0x7E: +256..448 ; 0xF8..0xFE: -256..-448 (finite, NOT NaN).
    # Decode in int32 to avoid uint underflow on (exp - 7).
    x = x.astype(np.int32)
    sign = np.where(x & 0x80, -1.0, 1.0)
    exp = (x >> 3) & 0xF
    man = x & 0x7
    normal = np.power(2.0, exp.astype(np.float32) - 7.0) * (1.0 + man / 8.0)
    out = np.where(exp == 0, 0.5 * (man / 8.0), normal)
    out = np.where((exp == 15) & (man == 7), 0.0, out)  # 0x7F/0xFF NaN guard
    return (sign * out).astype(np.float32)


def dequant_expert(pfx, proj, N, K):
    # weight: U8 [N, K/2], each byte = 2 FP4 (low nibble = even col, high = odd)
    w = read_tensor(f"{pfx}.{proj}.weight").astype(np.uint8)
    # weight_scale: [N, K/16] ROW-MAJOR in the checkpoint (the C++ loader
    # swizzles it to the 128x64 atom layout on the host at load time — see
    # moe_weights.cpp; the moe_load_packed_matches_shard test verifies the
    # inverse swizzle round-trips to the shard bytes). Decode directly.
    sf = read_tensor(f"{pfx}.{proj}.weight_scale").astype(np.uint8)
    ws2 = float(read_tensor(f"{pfx}.{proj}.weight_scale_2").ravel()[0])
    nib_lo = (w & 0x0F).astype(np.int64)
    nib_hi = ((w >> 4) & 0x0F).astype(np.int64)
    vals = np.empty((N, K), dtype=np.float32)
    vals[:, 0::2] = _E2M1[nib_lo]
    vals[:, 1::2] = _E2M1[nib_hi]
    sfv = np.repeat(_e4m3_decode(sf), 16, axis=1)
    return (vals * sfv * ws2).astype(np.float32)


_CFG = None  # set by build_model; used by _dequant_one_expert


def _dequant_one_expert(layer_id, expert_idx):
    """Dequantize one expert's gate/up/down projections to bf16 tensors.

    Returns (gu, dn) where gu is [2*mis, hs] (gate then up stacked, matching
    the model's gate_up_proj[expert] layout) and dn is [hs, mis].
    """
    import torch

    hs = _CFG.hidden_size
    mis = _CFG.moe_intermediate_size
    lp = f"model.language_model.layers.{layer_id}"
    ep = f"{lp}.mlp.experts.{expert_idx}"
    g = dequant_expert(ep, "gate_proj", mis, hs)  # [mis, hs]
    u = dequant_expert(ep, "up_proj", mis, hs)  # [mis, hs]
    d = dequant_expert(ep, "down_proj", hs, mis)  # [hs, mis]
    # Keep float32 to match the reference model's params (the model is built
    # in float32 and the eager path copies the float32 dequant values in
    # directly — no bf16 rounding). gu stacks gate then up to mirror the
    # gate_up_proj[expert] layout.
    gu = torch.from_numpy(np.concatenate([g, u], axis=0))
    dn = torch.from_numpy(d)
    return gu, dn


# ---------------------------------------------------------------------------
# PLE sidecar gather (FP8 e4m3 rows -> BF16)
# ---------------------------------------------------------------------------
def sidecar_rows(row_ids):
    row_ids = np.asarray(row_ids, dtype=np.int64)
    out = np.zeros((row_ids.size, ROW_BYTES), dtype=np.float32)
    with open(SIDECAR, "rb") as f:
        for idx, rid in enumerate(row_ids):
            if rid < 0 or rid >= TOTAL_ROWS:
                continue
            f.seek(rid * ROW_BYTES)
            raw = f.read(ROW_BYTES)
            out[idx] = _e4m3_decode(np.frombuffer(raw, dtype=np.uint8))
    return out


def make_ple_gather(ple_emb_module, weight_scale):
    """Replace ple_embedding forward with a sidecar-backed gather that
    returns the same [B, T, ple_embed_dim] BF16 tensor the nn.Embedding
    version would return (16 head rows concatenated)."""
    cfg = ple_emb_module
    ngram = cfg.ngram_size
    hpn = cfg.heads_per_ngram
    eos = cfg.eos_token_id
    mult = cfg.layer_multipliers  # [ngram] int64 tensor
    hvs = cfg.ngram_heads_vocab_sizes  # [16]
    hoff = cfg.ngram_heads_offsets  # [16]

    def shift_right_ignore_eos(token_ids, shift):
        if shift == 0:
            return token_ids
        B, L = token_ids.shape
        positions = torch.arange(L, dtype=torch.long)
        eos_positions = torch.where(token_ids == eos, positions, -1)
        prev_eos_incl = torch.cummax(eos_positions, dim=1).values
        prev_eos = torch.cat(
            [torch.full((B, 1), -1, dtype=torch.long), prev_eos_incl[:, :-1]],
            dim=1,
        )
        segment_start = prev_eos + 1
        pos_in_seg = positions.unsqueeze(0) - segment_start
        src = positions - shift
        gather_idx = src.clamp_min(0).unsqueeze(0).expand(B, -1)
        shifted = token_ids.gather(1, gather_idx)
        valid = (pos_in_seg >= shift) & (src.unsqueeze(0) >= 0)
        return torch.where(valid, shifted, torch.full_like(token_ids, eos))

    def forward(input_ids, past_key_values=None, *args, **kwargs):
        input_ids = input_ids.long()
        B, T = input_ids.shape
        # Mirror the real transformers NGramEmbedding.forward: the previous
        # context_len tokens come from the cache's conv_states[2] (set by
        # prior steps), else EOS padding (fresh prefill). This is what makes
        # the decode step (T=1) see the true n-gram history — matching the
        # C++ ModelDecodeStep `history` (prompt + previously decoded).
        layer_idx = cfg.layer_idx
        context_len = ngram - 1
        if past_key_values is not None and past_key_values.has_previous_state(
            layer_idx, state_idx=2
        ):
            previous_context = past_key_values.layers[layer_idx].conv_states[2].clone()
        else:
            previous_context = torch.full(
                (B, context_len), eos, dtype=torch.long, device=input_ids.device
            )
        if past_key_values is not None:
            input_ids_to_cache = input_ids
            if (
                not past_key_values.has_previous_state(layer_idx, state_idx=2)
                and T < context_len
            ):
                input_ids_to_cache = torch.nn.functional.pad(
                    input_ids_to_cache, (context_len - T, 0), value=eos
                )
            _ = past_key_values.update_conv_state(
                input_ids_to_cache,
                layer_idx,
                state_idx=2,
                conv_kernel_size=context_len,
            )
        token_history = torch.cat([previous_context, input_ids], dim=-1)
        shifted = [shift_right_ignore_eos(token_history, s) for s in range(ngram)]
        blocks = []
        for ngram_n in range(2, ngram + 1):
            s0 = (ngram_n - 2) * hpn
            s1 = s0 + hpn
            mixed = shifted[0] * mult[0]
            for p in range(1, ngram_n):
                mixed = torch.bitwise_xor(mixed, shifted[p] * mult[p])
            hvs_t = hvs[s0:s1]
            hoff_t = hoff[s0:s1]
            ngram_ids = torch.remainder(
                mixed.unsqueeze(-1), hvs_t.view(1, 1, -1)
            )
            blocks.append(ngram_ids + hoff_t.view(1, 1, -1))
        ngram_ids = torch.cat(blocks, dim=-1)[:, -T:]  # [B, T, 16]
        ids = ngram_ids[0].cpu().numpy().ravel()
        rows = sidecar_rows(ids)  # [T*16, 160] float32
        emb = torch.from_numpy((rows * weight_scale).astype(np.float32))
        emb = emb.view(B, T, -1)
        return emb

    return forward


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def build_model(n_layers):
    """Build the N-layer reference model (real weights) and return
    (model, lm_head_np, cfg). Reusable by ref4_decode.py (prefill + decode)."""
    cfg = Qwen4ExpTextConfig.from_pretrained(MDIR, trust_remote_code=True)
    cfg.num_hidden_layers = n_layers
    cfg._attn_implementation = "eager"
    global _CFG
    _CFG = cfg  # used by _dequant_one_expert
    print(
        f"config ok: layers {cfg.num_hidden_layers} types {cfg.layer_types[:n_layers]}",
        flush=True,
    )

    model = Qwen4ExpTextModel(cfg)
    model.eval()

    P = "model.language_model"

    def set(name, arr, t=None):
        if t is None:
            t = torch.from_numpy(arr).to(torch.bfloat16)
        with torch.no_grad():
            model.get_parameter(name).copy_(t)
        print("  set", name, tuple(t.shape), flush=True)

    print("loading head...", flush=True)
    set("embed_tokens.weight", read_tensor(f"{P}.embed_tokens.weight"))
    mix = f"{P}.hyper_connection_mixer"
    set(f"hyper_connection_mixer.hc_norm.weight", read_tensor(f"{mix}.hc_norm.weight"))
    set(
        f"hyper_connection_mixer.input_mix_weight_down.weight",
        read_tensor(f"{mix}.input_mix_weight_down.weight"),
    )
    set(
        f"hyper_connection_mixer.input_mix_weight_up.weight",
        read_tensor(f"{mix}.input_mix_weight_up.weight"),
    )

    for li in range(n_layers):
        lp = f"{P}.layers.{li}"
        ltype = cfg.layer_types[li]
        print(f"loading layer {li} ({ltype})...", flush=True)
        for part in (
            "hc_norm.weight",
            "input_mix_weight_down.weight",
            "input_mix_weight_up.weight",
            "block_inject_weight.weight",
        ):
            set(
                f"layers.{li}.attn_hyper_connection.{part}",
                read_tensor(f"{lp}.attn_hyper_connection.{part}"),
            )
            set(
                f"layers.{li}.mlp_hyper_connection.{part}",
                read_tensor(f"{lp}.mlp_hyper_connection.{part}"),
            )
        if ltype == "linear_attention":
            la = f"layers.{li}.linear_attn"
            for part in (
                "in_proj_qkv.weight",
                "in_proj_z.weight",
                "in_proj_b.weight",
                "in_proj_a.weight",
                "conv1d.weight",
                "dt_bias",
                "A_log",
                "norm.weight",
                "out_proj.weight",
            ):
                set(f"{la}.{part}", read_tensor(f"{lp}.linear_attn.{part}"))
        else:
            sa = f"layers.{li}.self_attn"
            for part in (
                "q_proj.weight",
                "k_proj.weight",
                "v_proj.weight",
                "o_proj.weight",
                "q_norm.weight",
                "k_norm.weight",
                "indexer.index_qk_proj.weight",
                "indexer.q_layernorm.weight",
                "indexer.k_layernorm.weight",
            ):
                set(f"{sa}.{part}", read_tensor(f"{lp}.self_attn.{part}"))
        # MoE: gate + shared expert (BF16) + routed (NVFP4 dequant)
        set(f"layers.{li}.mlp.gate.weight", read_tensor(f"{lp}.mlp.gate.weight"))
        set(
            f"layers.{li}.mlp.shared_expert.gate_proj.weight",
            read_tensor(f"{lp}.mlp.shared_expert.gate_proj.weight"),
        )
        set(
            f"layers.{li}.mlp.shared_expert.up_proj.weight",
            read_tensor(f"{lp}.mlp.shared_expert.up_proj.weight"),
        )
        set(
            f"layers.{li}.mlp.shared_expert.down_proj.weight",
            read_tensor(f"{lp}.mlp.shared_expert.down_proj.weight"),
        )
        set(
            f"layers.{li}.mlp.shared_expert_gate.weight",
            read_tensor(f"{lp}.mlp.shared_expert_gate.weight"),
        )
        # Routed experts are loaded lazily during forward (see
        # _patched_experts_forward): only the layer's _layer_id is recorded so
        # the on-demand dequant can locate the shard prefix. This keeps peak
        # memory at ~15 GB instead of ~5 GB per resident layer.
        model.layers[li].mlp.experts._layer_id = li
        print(f"  layer {li} routed experts: lazy (dequant on demand)", flush=True)

    # PLE layer (ple_layer_ids is 1-indexed in the checkpoint; layer 1 0-indexed).
    ple_layer_idx = cfg.ple_layer_ids[0] - 1
    if ple_layer_idx < n_layers:
        pl = f"{P}.layers.{ple_layer_idx}.ple"
        ple = model.layers[ple_layer_idx].ple
        for part in (
            "key_proj.weight",
            "value_proj.weight",
            "norm_key.weight",
            "norm_query.weight",
            "norm_conv.weight",
            "conv1d.weight",
        ):
            set(f"layers.{ple_layer_idx}.ple.{part}", read_tensor(f"{pl}.{part}"))
        ws_t = read_tensor(f"{pl}.ple_embedding.ngram_embedding.weight_scale")
        ple_ws = float(ws_t.ravel()[0])
        print("ple weight_scale:", ple_ws, flush=True)
        emb_mod = ple.ple_embedding
        print(
            "hash params: multipliers",
            emb_mod.layer_multipliers.tolist(),
            "head_sizes[:3]",
            emb_mod.ngram_heads_vocab_sizes[:3].tolist(),
            "offsets[:3]",
            emb_mod.ngram_heads_offsets[:3].tolist(),
            flush=True,
        )
        ple_emb_forward = make_ple_gather(emb_mod, ple_ws)
        # Assign as an instance attribute (plain function, not bound via
        # __get__), so self.forward(input_ids, past_key_values) calls it with
        # exactly those.
        emb_mod.forward = ple_emb_forward

    # lm_head
    lm_w = read_tensor("lm_head.weight")
    print("lm_head loaded", lm_w.shape, flush=True)
    return model, lm_w, cfg


def main():
    out_prefix = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ref4"
    model, lm_w, cfg = build_model(N_LAYERS)

    # Optional prompt file (one int32 per line) overrides the fixed IDS, so a
    # long prefill (e.g. 256 tokens) can be compared against the C++ engine on
    # the identical token sequence.
    ids_list = IDS
    pf = os.environ.get("Q4T_REF_PROMPT_FILE")
    if pf:
        with open(pf) as f:
            ids_list = [int(x) for x in f.read().split()]
        print(f"prompt file {pf}: {len(ids_list)} tokens", flush=True)
    ids = torch.tensor([ids_list], dtype=torch.long)
    with torch.no_grad():
        hs_out = model(input_ids=ids)
    hidden = hs_out.last_hidden_state[0]  # [T, hs]
    logits = (hidden.float() @ torch.from_numpy(lm_w).float().T).numpy()
    np.save(out_prefix + ".logits.npy", logits)
    print("logits shape:", logits.shape, flush=True)
    print("max_abs:", float(np.abs(logits).max()), flush=True)
    for t in range(len(IDS)):
        top = np.argsort(logits[t])[-5:][::-1]
        print(
            f"  token {t} (id {IDS[t]}): argmax={int(logits[t].argmax())} "
            f"top5={list(map(int, top))}",
            flush=True,
        )
    print("saved:", out_prefix + ".logits.npy", flush=True)


if __name__ == "__main__":
    main()
