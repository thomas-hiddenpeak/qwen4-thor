#!/usr/bin/env python3
"""CPU reference for the Qwen4Exp vision tower (Qwen3-VL ViT).

Implements the full vision-tower forward in NumPy (no torch), loads the real
`model.visual.*` weights from the checkpoint, and runs it on a synthetic test
image. The output is saved as JSON for the C++ test to compare against.

Vision tower (reference/vllm/vllm/model_executor/models/qwen3_vl.py):
  pixel_values [L, C*T*P*P]  (L = t*h*w patches, layout [C, T, P, P] per patch)
    -> patch_embed Conv3d [H, C, T, P, P] (t=1 image: 2D conv over P x P)
    -> + pos_embed (bilinear-interpolated from the 48x48 grid)
    -> 27 blocks: x += attn(LN1(x)); x += mlp(LN2(x))
         attn: qkv [3H, H] -> 16 heads (head_dim 72) -> 2D RoPE (partial 0.5)
               -> bidirectional softmax attention (per image) -> proj [H, H]
         mlp: fc1 [I, H] -> GELU(tanh) -> fc2 [H, I]
    -> merger: 2x2 spatial merge -> LN -> fc1 [4H, 4H] -> GELU -> fc2 [out, 4H]
    -> [h/2 * w/2, out_hidden_size]  (out_hidden_size = 2560 = main hs)

Layout convention (must match the C++ kernel):
  - pixel_values[p, c*T*P*P + (t*P + i)*P + j] for patch p, channel c,
    temporal t, pixel (i, j). For t=1 this is [C, P, P] per patch.
  - patch order within an image: row-major over the h x w patch grid.
  - qkv weight [3H, H] is stacked [q; k; v], each [H, H].
"""
import json
import math
import os
import struct
import sys

import numpy as np

MODEL_DIR = (
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream"
)
OUT_DIR = os.path.dirname(os.path.abspath(__file__))

# Vision config (from config.json vision_config).
DEPTH = 27
HIDDEN = 1152
NUM_HEADS = 16
HEAD_DIM = HIDDEN // NUM_HEADS  # 72
INTERMEDIATE = 4304
PATCH = 16
TEMPORAL = 2
MERGE = 2
OUT_HIDDEN = 2560
NUM_POS_EMBED = 2304
GRID = int(NUM_POS_EMBED ** 0.5)  # 48
EPS = 1e-6
ROPE_THETA = 10000.0


def read_safetensors_header(path):
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        return json.loads(f.read(n))


def make_loader():
    idx = json.load(open(os.path.join(MODEL_DIR, "model.safetensors.index.json")))
    wm = idx["weight_map"]
    base = MODEL_DIR + "/"
    cache = {}

    def load(name):
        if name in cache:
            return cache[name]
        shard = wm[name]
        path = base + shard
        if shard not in cache:
            with open(path, "rb") as f:
                n = struct.unpack("<Q", f.read(8))[0]
                header = json.loads(f.read(n))
                data_start = 8 + n
            cache[shard] = (header, data_start)
        header, data_start = cache[shard]
        info = header[name]
        dtype = info["dtype"]
        shape = info["shape"]
        nbytes = int(np.prod(shape)) * (2 if dtype == "BF16" else 4)
        with open(path, "rb") as f:
            f.seek(data_start + info["data_offsets"][0])
            raw = f.read(nbytes)
        if dtype == "BF16":
            # BF16 -> FP32: take the high 16 bits, reinterpret as float32.
            u = np.frombuffer(raw, dtype=np.uint16).astype(np.uint32) << 16
            arr = u.view(np.float32).reshape(shape)
        else:
            arr = np.frombuffer(raw, dtype=np.float32).reshape(shape)
        cache[name] = arr
        return arr

    return load


def bf16_round(x):
    """Round FP32 to BF16 precision (for matching the C++ BF16 storage)."""
    u = x.astype(np.float32).view(np.uint32)
    # Round-to-nearest-even on the low 16 bits.
    u = (u + 0x7FFF + ((u >> 16) & 1)) & 0xFFFF0000
    return u.view(np.float32)


def gelu_tanh(x):
    """GELU tanh approximation (gelu_pytorch_tanh)."""
    c = math.sqrt(2.0 / math.pi)
    inner = c * (x + 0.044715 * x ** 3)
    return 0.5 * x * (1.0 + np.tanh(inner))


def layer_norm(x, weight, bias, eps=EPS):
    mean = x.mean(axis=-1, keepdims=True)
    var = x.var(axis=-1, keepdims=True)
    return weight * (x - mean) / np.sqrt(var + eps) + bias


def matmul(x, w, b=None):
    """y = x @ W^T + b, with W [N, K] (checkpoint layout)."""
    y = x @ w.T
    if b is not None:
        y = y + b
    return y


def patch_embed(x, w, b):
    """x [L, C*T*P*P] -> [L, H]. w [H, C, T, P, P]."""
    L = x.shape[0]
    C, T, P = w.shape[1], w.shape[2], w.shape[3]
    x = x.reshape(L, C, T, P, P)
    # Conv3d with kernel (T, P, P), stride (T, P, P): each patch -> 1 output.
    # out[o] = sum_{c,t,i,j} w[o, c, t, i, j] * x[..., c, t, i, j]
    out = np.tensordot(x, w, axes=([1, 2, 3, 4], [1, 2, 3, 4]))  # [L, H]
    out = out + b
    return out


def pos_embed_interpolate(w, t, h, w_grid):
    """Bilinear-interpolate the [GRID*GRID, H] pos embed to [t*h*w_grid, H]."""
    h_idxs = np.linspace(0, GRID - 1, h, dtype=np.float32)
    w_idxs = np.linspace(0, GRID - 1, w_grid, dtype=np.float32)
    h_floor = h_idxs.astype(np.int64)
    w_floor = w_idxs.astype(np.int64)
    h_ceil = np.minimum(h_floor + 1, GRID - 1)
    w_ceil = np.minimum(w_floor + 1, GRID - 1)
    dh = h_idxs - h_floor
    dw = w_idxs - w_floor
    dh_g, dw_g = np.meshgrid(dh, dw, indexing="ij")  # [h, w]
    hf_g, wf_g = np.meshgrid(h_floor, w_floor, indexing="ij")
    hc_g, wc_g = np.meshgrid(h_ceil, w_ceil, indexing="ij")
    w11 = dh_g * dw_g
    w10 = dh_g - w11
    w01 = dw_g - w11
    w00 = 1 - dh_g - w01
    # 4 corners: (hf,wf), (hf,wc), (hc,wf), (hc,wc)
    h_grid = np.stack([hf_g, hf_g, hc_g, hc_g])  # [4, h, w]
    w_grid_arr = np.stack([wf_g, wc_g, wf_g, wc_g])
    indices = (h_grid * GRID + w_grid_arr).reshape(4, -1)  # [4, h*w]
    weights = np.stack([w00, w01, w10, w11], axis=0).reshape(4, -1, 1)
    corners = w[indices]  # [4, h*w, H]
    combined = (corners * weights).sum(axis=0)  # [h*w, H]
    # Spatial-merge order: reshape [h/m, m, w/m, m, H] -> permute -> flatten.
    m = MERGE
    combined = combined.reshape(h // m, m, w_grid // m, m, HIDDEN)
    combined = combined.transpose(0, 2, 1, 3, 4).reshape(1, -1, HIDDEN)
    # Tile the spatial table t times (time-major: l = g*(h*w) + s). This matches
    # vLLM's `combined.expand(t, -1, -1)` (a block repeat), NOT np.repeat
    # (which would repeat each row consecutively). For t=1 they are identical.
    return np.tile(combined, (t, 1)).reshape(-1, HIDDEN)


def rot_pos_ids(h, w):
    """2D position ids [h*w, 2] in spatial-merge order."""
    m = MERGE
    hpos = np.broadcast_to(np.arange(h).reshape(h, 1), (h, w))
    wpos = np.broadcast_to(np.arange(w).reshape(1, w), (h, w))
    hpos = hpos.reshape(h // m, m, w // m, m).transpose(0, 2, 1, 3).flatten()
    wpos = wpos.reshape(h // m, m, w // m, m).transpose(0, 2, 1, 3).flatten()
    return np.stack([hpos, wpos], axis=-1).astype(np.int64)


def rot_pos_emb(pos_ids):
    """2D RoPE cos/sin from position ids [L, 2] (h_pos, w_pos).

    The head_dim (72) is split into two 36-dim halves. Each half gets a
    neox-style rotation (rotary_dim=36, pairing (i, i+18) for i in [0,18)):
      - first half (dims 0..35) uses h_pos
      - second half (dims 36..71) uses w_pos
    Returns (cos_h, sin_h, cos_w, sin_w), each [L, 18].
    """
    rotary_dim = HEAD_DIM // 2  # 36
    neox_half = rotary_dim // 2  # 18
    inv_freq = 1.0 / (
        ROPE_THETA ** (np.arange(0, neox_half, dtype=np.float32) * 2.0 / rotary_dim)
    )
    h_pos = pos_ids[:, 0].astype(np.float32)  # [L]
    w_pos = pos_ids[:, 1].astype(np.float32)  # [L]
    cos_h = np.cos(np.outer(h_pos, inv_freq))  # [L, 18]
    sin_h = np.sin(np.outer(h_pos, inv_freq))
    cos_w = np.cos(np.outer(w_pos, inv_freq))
    sin_w = np.sin(np.outer(w_pos, inv_freq))
    return cos_h, sin_h, cos_w, sin_w


def apply_rope(x, cos_h, sin_h, cos_w, sin_w):
    """x [L, NUM_HEADS, HEAD_DIM] -> 2D RoPE (h on first half, w on second)."""
    nh = HEAD_DIM // 2  # 36
    neox_half = nh // 2  # 18
    # First half (h): neox rotation pairing (i, i+18).
    x1 = x[..., :neox_half]  # [L, NH, 18]
    x2 = x[..., neox_half:nh]  # [L, NH, 18]
    ch = cos_h[:, None, :]  # [L, 1, 18]
    sh = sin_h[:, None, :]
    out1 = x1 * ch - x2 * sh
    out2 = x2 * ch + x1 * sh
    # Second half (w).
    x3 = x[..., nh : nh + neox_half]  # [L, NH, 18]
    x4 = x[..., nh + neox_half :]  # [L, NH, 18]
    cw = cos_w[:, None, :]
    sw = sin_w[:, None, :]
    out3 = x3 * cw - x4 * sw
    out4 = x4 * cw + x3 * sw
    return np.concatenate([out1, out2, out3, out4], axis=-1)


def vision_attention(x, qkv_w, qkv_b, proj_w, proj_b, rope, t=1):
    """x [L, H] -> [L, H]. Bidirectional attention, PER TIME GROUP.

    Layout is time-major: L = t * (h*w), tokens of time group g are the
    consecutive slice [g*(h*w), (g+1)*(h*w)). The reference (vLLM
    qwen3_vl.py prepare_encoder_metadata) builds cu_seqlens =
    repeat(h*w, t), i.e. each time group is its own attention sequence and
    groups do NOT attend to each other. For t=1 this is identical to a
    single bidirectional attention over all L tokens.
    """
    L = x.shape[0]
    qkv = matmul(x, qkv_w, qkv_b)  # [L, 3H]
    qkv = qkv.reshape(L, 3, NUM_HEADS, HEAD_DIM)
    q = qkv[:, 0]  # [L, NH, HD]
    k = qkv[:, 1]
    v = qkv[:, 2]
    q = apply_rope(q, *rope)
    k = apply_rope(k, *rope)
    scale = HEAD_DIM ** -0.5
    # Per time group: scores[g, i, h, j] = q[g,i,h] . k[g,j,h].
    qg = q.reshape(t, -1, NUM_HEADS, HEAD_DIM)
    kg = k.reshape(t, -1, NUM_HEADS, HEAD_DIM)
    vg = v.reshape(t, -1, NUM_HEADS, HEAD_DIM)
    scores = np.einsum("gihd,gjhd->gihj", qg, kg) * scale  # [t, S, NH, S]
    attn = np.exp(scores - scores.max(axis=-1, keepdims=True))
    attn = attn / attn.sum(axis=-1, keepdims=True)
    context = np.einsum("gihj,gjhd->gihd", attn, vg)  # [t, S, NH, HD]
    context = context.reshape(L, HIDDEN)
    return matmul(context, proj_w, proj_b)


def vision_block(x, rope, b, t=1):
    x = x + vision_attention(
        layer_norm(x, b["norm1_w"], b["norm1_b"]),
        b["qkv_w"], b["qkv_b"], b["proj_w"], b["proj_b"], rope, t,
    )
    h = layer_norm(x, b["norm2_w"], b["norm2_b"])
    mlp = gelu_tanh(matmul(h, b["fc1_w"], b["fc1_b"]))
    x = x + matmul(mlp, b["fc2_w"], b["fc2_b"])
    return x


def merger(x, m):
    """x [L, H] (L = h*w, block-major order) -> [h/2 * w/2, OUT_HIDDEN].

    The main merger uses use_postshuffle_norm=False: LayerNorm is applied to
    the 1152-dim tokens BEFORE the 2x2 spatial merge (view to 4608).
    Since x is in block-major order, the 4 tokens of each 2x2 block are
    already consecutive, so the merge is just a reshape (no transpose).
    """
    x = layer_norm(x, m["norm_w"], m["norm_b"])  # [L, 1152]
    x = x.reshape(-1, HIDDEN * MERGE * MERGE)  # [L/4, 4608]
    x = gelu_tanh(matmul(x, m["fc1_w"], m["fc1_b"]))
    return matmul(x, m["fc2_w"], m["fc2_b"])


def load_vision_weights(load):
    """Load all model.visual.* weights into a dict of blocks + top-level."""
    vis = {}
    for i in range(DEPTH):
        p = f"model.visual.blocks.{i}."
        vis[i] = {
            "norm1_w": load(p + "norm1.weight"),
            "norm1_b": load(p + "norm1.bias"),
            "norm2_w": load(p + "norm2.weight"),
            "norm2_b": load(p + "norm2.bias"),
            "qkv_w": load(p + "attn.qkv.weight"),
            "qkv_b": load(p + "attn.qkv.bias"),
            "proj_w": load(p + "attn.proj.weight"),
            "proj_b": load(p + "attn.proj.bias"),
            "fc1_w": load(p + "mlp.linear_fc1.weight"),
            "fc1_b": load(p + "mlp.linear_fc1.bias"),
            "fc2_w": load(p + "mlp.linear_fc2.weight"),
            "fc2_b": load(p + "mlp.linear_fc2.bias"),
        }
    vis["patch_w"] = load("model.visual.patch_embed.proj.weight")
    vis["patch_b"] = load("model.visual.patch_embed.proj.bias")
    vis["pos_w"] = load("model.visual.pos_embed.weight")
    vis["merger"] = {
        "norm_w": load("model.visual.merger.norm.weight"),
        "norm_b": load("model.visual.merger.norm.bias"),
        "fc1_w": load("model.visual.merger.linear_fc1.weight"),
        "fc1_b": load("model.visual.merger.linear_fc1.bias"),
        "fc2_w": load("model.visual.merger.linear_fc2.weight"),
        "fc2_b": load("model.visual.merger.linear_fc2.bias"),
    }
    return vis


def vision_forward(vis, pixel_values, grid_thw):
    """pixel_values [L, C*T*P*P], grid_thw [[t, h, w]] -> [h/2*w/2, OUT_HIDDEN]."""
    t, h, w = grid_thw[0]
    L = t * h * w
    x = patch_embed(pixel_values, vis["patch_w"], vis["patch_b"])
    pos = pos_embed_interpolate(vis["pos_w"], t, h, w)
    x = x + pos
    pos_ids = rot_pos_ids(h, w)
    if t > 1:
        # Tile (block repeat, time-major) to match vLLM's `.repeat(t, 1)`, NOT
        # np.repeat (element-wise repeat). For t=1 they are identical.
        pos_ids = np.tile(pos_ids, (t, 1))
    rope = rot_pos_emb(pos_ids)
    for i in range(DEPTH):
        x = vision_block(x, rope, vis[i], t)
    return merger(x, vis["merger"])


def make_pixels(t, h, w, seed):
    """Synthetic pixel_values [L, C*T*P*P] in time-major + block-major order.

    Time-major: token l = g*(h*w) + s, g in [0,t), s in [0,h*w) (spatial).
    Within each time group, spatial patches are in block-major (spatial-merge)
    order. Each time group gets its OWN random values (seed + g) so the video
    groups are distinct (unlike a still image where all temporal slots repeat
    the same frame).
    """
    L = t * h * w
    C, P = 3, PATCH
    m = MERGE
    S = h * w
    pixel = np.empty((L, C * TEMPORAL * P * P), dtype=np.float32)
    for g in range(t):
        rng = np.random.default_rng(seed + g)
        # Generate this group's S spatial patches in row-major, then reorder
        # to block-major.
        rm = rng.standard_normal((S, C * TEMPORAL * P * P)).astype(np.float32)
        bm = np.empty_like(rm)
        for hb in range(h // m):
            for wb in range(w // m):
                for mb in range(m):
                    for mj in range(m):
                        i = hb * m + mb
                        j = wb * m + mj
                        p_rm = i * w + j
                        p_bm = (hb * (w // m) + wb) * (m * m) + mb * m + mj
                        bm[p_bm] = rm[p_rm]
        pixel[g * S : (g + 1) * S] = bm
    return pixel


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(OUT_DIR, "vision_ref.json")
    print("Loading vision weights from", MODEL_DIR, flush=True)
    load = make_loader()
    vis = load_vision_weights(load)
    print("  weights loaded", flush=True)

    # Two cases: a still image (t=1, 4x4 patches) and a short video (t=2,
    # 4x4 patches per frame -> 2 time groups). Both exercise the same tower;
    # the video case exercises per-time-group attention.
    cases = {"image": (1, 4, 4, 1234), "video": (2, 4, 4, 777)}
    data = {}
    for name, (t, h, w, seed) in cases.items():
        L = t * h * w
        pixel_values = make_pixels(t, h, w, seed)
        print("Running %s forward (L=%d, grid=%dx%dx%d)..." % (name, L, t, h, w),
              flush=True)
        out_feat = vision_forward(vis, pixel_values, [[t, h, w]])
        print("  output shape:", out_feat.shape, flush=True)
        data[name] = {
            "grid_thw": [[t, h, w]],
            "pixel_values": pixel_values.tolist(),
            "output": out_feat.tolist(),
            "output_shape": list(out_feat.shape),
        }
        print("  output[0, :4] =", out_feat[0, :4].tolist(), flush=True)
        print("  output max_abs =", float(np.abs(out_feat).max()), flush=True)

    with open(out, "w") as f:
        json.dump(data, f)
    print("Saved reference to", out, flush=True)


if __name__ == "__main__":
    main()
