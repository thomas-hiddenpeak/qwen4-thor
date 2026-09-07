#!/usr/bin/env python3
"""Ground-truth image processor reference (transformers 5.16.1, PIL backend).

Uses the real `Qwen2VLImageProcessorPil` (the exact processor the Qwen3-VL
model expects) to turn a synthetic RGB image into `pixel_values` +
`image_grid_thw`. This is the authoritative reference for the C++ image
processor (q4t_vision processor).

Two things are verified/emitted:
  1. ORDER: we independently patchify the resized+normalized image in both
     block-major and row-major order and check which one matches the
     processor's `pixel_values` exactly. This proves the C++ processor must
     emit block-major (the order the vision tower + pos_embed + merger use).
  2. GROUND TRUTH: the processor's `pixel_values` + `image_grid_thw` are saved
     to tools/vision_processor_ref.json for the C++ test to compare against.

Run with the refenv python (has transformers + PIL + numpy):
  .q4t-work/refenv/bin/python tools/vision_processor_ref.py
"""
import json
import os

import numpy as np
from PIL import Image

from transformers.models.qwen2_vl import Qwen2VLImageProcessorPil

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "vision_processor_ref.json")

# Processor config (from the model's preprocessor_config.json).
PATCH = 16
TEMPORAL = 2
MERGE = 2
MIN_PIXELS = 65536       # size.shortest_edge
MAX_PIXELS = 16777216    # size.longest_edge
MEAN = [0.5, 0.5, 0.5]
STD = [0.5, 0.5, 0.5]


def make_processor():
    return Qwen2VLImageProcessorPil(
        size={"shortest_edge": MIN_PIXELS, "longest_edge": MAX_PIXELS},
        patch_size=PATCH,
        temporal_patch_size=TEMPORAL,
        merge_size=MERGE,
        image_mean=MEAN,
        image_std=STD,
        do_resize=True,
        do_rescale=True,
        do_normalize=True,
        do_convert_rgb=True,
    )


def smart_resize(height, width, factor=32, min_pixels=MIN_PIXELS,
                 max_pixels=MAX_PIXELS):
    """Reference copy of transformers smart_resize (for the independent
    patchify check below)."""
    import math
    if max(height, width) / min(height, width) > 200:
        raise ValueError("aspect ratio too large")
    h_bar = round(height / factor) * factor
    w_bar = round(width / factor) * factor
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = max(factor, math.floor(height / beta / factor) * factor)
        w_bar = max(factor, math.floor(width / beta / factor) * factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = math.ceil(height * beta / factor) * factor
        w_bar = math.ceil(width * beta / factor) * factor
    return h_bar, w_bar


def manual_patchify_blockmajor(rgb01, h, w):
    """Independent block-major patchify of a [h, w, 3] float image in [0,1].

    Returns [grid_h*grid_w, 3*TEMPORAL*PATCH*PATCH] in block-major order,
    per-patch layout [C, T, P, P] (single frame repeated TEMPORAL times).
    """
    g_h, g_w = h // PATCH, w // PATCH
    m = MERGE
    # normalized to [-1,1]: (x - 0.5)/0.5 = 2x - 1
    x = 2.0 * rgb01 - 1.0
    out = np.empty((g_h * g_w, 3 * TEMPORAL * PATCH * PATCH), dtype=np.float32)
    for hb in range(g_h // m):
        for wb in range(g_w // m):
            for mb in range(m):
                for mj in range(m):
                    i = hb * m + mb      # patch row
                    j = wb * m + mj      # patch col
                    p_idx = (hb * (g_w // m) + wb) * (m * m) + mb * m + mj
                    tile = x[i * PATCH:(i + 1) * PATCH,
                             j * PATCH:(j + 1) * PATCH, :]  # [P,P,3]
                    # [C, T, P, P]: channel-major, temporal (repeat), then P,P
                    arr = np.empty((3, TEMPORAL, PATCH, PATCH), dtype=np.float32)
                    for cc in range(3):
                        arr[cc, 0] = tile[:, :, cc]
                        if TEMPORAL > 1:
                            arr[cc, 1] = tile[:, :, cc]
                    out[p_idx] = arr.reshape(-1)
    return out, g_h, g_w


def l2_rel(a, b):
    a = a.astype(np.float64).ravel()
    b = b.astype(np.float64).ravel()
    d = np.linalg.norm(a - b)
    n = np.linalg.norm(b)
    return float(d / n) if n > 0 else float(d)


def gen_image_a():
    """32-multiple image (256x256 = 65536 px, == min_pixels) -> identity resize
    (exact). This is the strongest test: C++ decode + normalize + patchify must
    match the processor bit-for-bit."""
    rng = np.random.default_rng(11)
    h0, w0 = 256, 256
    yy, xx = np.mgrid[0:h0, 0:w0]
    img = np.zeros((h0, w0, 3), dtype=np.float32)
    img[:, :, 0] = xx / w0
    img[:, :, 1] = yy / h0
    img[:, :, 2] = 0.5 + 0.5 * np.sin(xx / 5.0) * np.cos(yy / 7.0)
    img[32:96, 32:128, :] = 0.9
    img[160:224, 128:224, :] = 0.1
    img = np.clip(img + 0.02 * rng.standard_normal(img.shape), 0.0, 1.0)
    return (img * 255).round().astype(np.uint8)


def gen_image_b():
    """Non-32-multiple image (100x140) -> real bicubic resize."""
    rng = np.random.default_rng(20240907)
    h0, w0 = 100, 140
    yy, xx = np.mgrid[0:h0, 0:w0]
    img = np.zeros((h0, w0, 3), dtype=np.float32)
    img[:, :, 0] = xx / w0
    img[:, :, 1] = yy / h0
    img[:, :, 2] = 0.5 + 0.5 * np.sin(xx / 7.0) * np.cos(yy / 5.0)
    img[10:30, 10:40, :] = 0.9
    img[60:90, 90:130, :] = 0.1
    img = np.clip(img + 0.01 * rng.standard_normal(img.shape), 0.0, 1.0)
    return (img * 255).round().astype(np.uint8)


def process_and_save(name, rgb_uint8):
    """Run the real processor, save PNG + ground-truth JSON."""
    pil_img = Image.fromarray(rgb_uint8, "RGB")
    h0, w0 = rgb_uint8.shape[0], rgb_uint8.shape[1]
    png_path = os.path.join(OUT_DIR, f"vision_test_{name}.png")
    pil_img.save(png_path)

    proc = make_processor()
    out = proc.preprocess(pil_img, return_tensors="np")
    pixel_values = np.asarray(out["pixel_values"])
    grid_thw = np.asarray(out["image_grid_thw"])[0].tolist()
    t, g_h, g_w = grid_thw
    L = pixel_values.shape[0]

    # Independent block-major check (proves order).
    rh, rw = smart_resize(h0, w0, factor=PATCH * MERGE)
    resized = np.asarray(pil_img.resize((rw, rh), Image.BICUBIC),
                         dtype=np.float32) / 255.0
    bm, _, _ = manual_patchify_blockmajor(resized, rh, rw)
    bm_rel = l2_rel(bm, pixel_values)

    # Ground truth as a plain-text file (trivially parseable by the C++ test):
    #   line 1: grid_t grid_h grid_w L patch_dim
    #   line 2..: flat pixel_values (L * patch_dim floats), space-separated
    gt_path = os.path.join(OUT_DIR, f"vision_proc_gt_{name}.txt")
    with open(gt_path, "w") as f:
        f.write(f"{t} {g_h} {g_w} {L} {pixel_values.shape[1]}\n")
        flat = pixel_values.astype(np.float32).ravel()
        # 100 floats per line for readability.
        for i in range(0, flat.size, 100):
            f.write(" ".join(repr(float(v)) for v in flat[i:i + 100]) + "\n")
    print(f"[{name}] {w0}x{h0} -> {rw}x{rh} (grid {g_w}x{g_h}), L={L}, "
          f"identity={rh == h0 and rw == w0}, bm_l2_rel={bm_rel:.2e}")
    return png_path, gt_path


if __name__ == "__main__":
    OUT_DIR = os.path.dirname(os.path.abspath(__file__))
    process_and_save("a", gen_image_a())
    process_and_save("b", gen_image_b())
    print("done")
