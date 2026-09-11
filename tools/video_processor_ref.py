#!/usr/bin/env python3
"""Ground-truth reference for the Qwen3-VL video processor.

Uses the real transformers 5.16.1 Qwen3VLProcessor (torchvision backend) to
produce the canonical `pixel_values_videos` + `video_grid_thw` for a set of
synthetic frame sequences. The C++ video processor (tools/video_processor_test)
is diffed against this output.

NOTE on resize backend: the video processor resizes with torchvision
`tvF.resize(..., BICUBIC, antialias=True)`, whereas the IMAGE processor uses
Pillow BICUBIC (which the C++ replicates bit-exactly). Measured difference
between the two BICUBIC backends on a random 320x224 image: max_abs_diff = 1
/255, l2_rel = 4.7e-5 (0.01% of pixels off by 0.5). After /255 + (x-0.5)/0.5
normalization that is ~0.004 — far below the BF16 precision band (~0.03). So
the C++ video processor reuses the existing Pillow 12.3.0 fixed-point BICUBIC
and the diff test uses a tolerance (max_abs_diff <= 1/255 in pixel space)
instead of a bit-exact match.

Pipeline (must match transformers Qwen3VLVideoProcessor):
  1. Frame sampling: indices = round(linspace(0, total-1, num_frames))
  2. smart_resize(h, w, num_frames, factor=32, temporal_factor=2,
     min_pixels=65536, max_pixels=16777216) -> (rh, rw)  [t*h*w budget]
  3. Per-frame BICUBIC resize (torchvision, antialias) -> uint8 [F, rh, rw, 3]
  4. Odd-frame pad: repeat LAST frame until F is even
  5. rescale /255 -> normalize (x-0.5)/0.5
  6. patchify: [B, grid_t, 2, C, gh/2, 2, 16, gw/2, 2, 16]
     -> permute(0,1,4,7,5,8,3,2,6,9) -> [B, grid_t*gh*gw, C*2*16*16]

Usage:
  python3 tools/video_processor_ref.py [--out tools/video_processor_ref.json]
"""

import argparse
import json
import sys

import numpy as np
from PIL import Image
from transformers import Qwen3VLProcessor

MODEL_DIR = (
    "/home/rm01/models/dev/llm/garnermccloud/"
    "Qwen3.8-Flash-Next-NVFP4-SSD-Stream"
)


def make_frames(seed: int, n: int, h: int, w: int):
    """Deterministic synthetic RGB frames [n, h, w, 3] uint8."""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, (n, h, w, 3), dtype=np.uint8)


def process(proc, frames):
    """Run the real video processor on `frames` (PIL list), return arrays.

    do_sample_frames=False: skip fps-based sampling so ALL provided frames are
    used (num_frames = len(frames), taken from the tensor shape). This makes the
    frame count deterministic and matches what the C++ processor will do (it
    receives an explicit frame list).
    """
    pil_frames = [Image.fromarray(f) for f in frames]
    out = proc(videos=[pil_frames], return_tensors="np",
               do_sample_frames=False)
    pv = out["pixel_values_videos"]  # [L, C*T*P*P]
    grid = out["video_grid_thw"]  # [1, 3] = [t, h, w]
    return np.asarray(pv, dtype=np.float32), np.asarray(grid, dtype=np.int64)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tools/video_processor_ref.json")
    args = ap.parse_args()

    proc = Qwen3VLProcessor.from_pretrained(MODEL_DIR)
    vp = proc.video_processor
    print(f"video processor: {type(vp).__name__}")
    print(f"  size={vp.size} min_frames={vp.min_frames} max_frames={vp.max_frames}")
    print(f"  patch={vp.patch_size} temporal={vp.temporal_patch_size} "
          f"merge={vp.merge_size}")

    cases = {
        # (seed, num_frames, h, w) — all frames used (do_sample_frames=False).
        # 224x320 / 448x640 are 32-multiples -> identity resize (bit-exact).
        # 225x321 is NOT -> real BICUBIC (validates the torchvision-vs-Pillow
        # backend tolerance).
        "v2_224x320": dict(seed=1, total=2, h=224, w=320),
        "v4_224x320": dict(seed=2, total=4, h=224, w=320),
        "v8_224x320": dict(seed=3, total=8, h=224, w=320),
        "v5_odd_224x320": dict(seed=4, total=5, h=224, w=320),
        "v4_448x640": dict(seed=5, total=4, h=448, w=640),
        "v4_225x321_bicubic": dict(seed=6, total=4, h=225, w=321),
    }

    result = {}
    for name, c in cases.items():
        frames = make_frames(c["seed"], c["total"], c["h"], c["w"])
        # Save each frame as a lossless PNG so the C++ test decodes the
        # EXACT same uint8 pixels (stb_image decode of a lossless PNG is
        # bit-exact). This isolates the processor (resize+normalize+patchify)
        # from any RNG differences between numpy and C++.
        for fi in range(c["total"]):
            Image.fromarray(frames[fi]).save(
                f"tools/video_frames_{name}_{fi}.png")
        pv, grid = process(proc, frames)
        t, gh, gw = [int(x) for x in grid[0]]
        L = t * gh * gw
        assert pv.shape[0] == L, f"{name}: pv {pv.shape[0]} != grid L {L}"
        # grid_t must equal ceil(total/2) (odd frames pad with the last frame)
        expect_t = (c["total"] + 1) // 2
        assert t == expect_t, f"{name}: grid_t {t} != expected {expect_t}"
        print(f"  {name}: F={c['total']} grid=[{t},{gh},{gw}] L={L} "
              f"pv={pv.shape} min={pv.min():.4f} max={pv.max():.4f}")
        result[name] = {
            "seed": c["seed"],
            "num_frames": c["total"],
            "frame_h": c["h"],
            "frame_w": c["w"],
            "grid_thw": [t, gh, gw],
            "patch_dim": int(pv.shape[1]),
            "pixel_values": pv.tolist(),
        }

    with open(args.out, "w") as f:
        json.dump(result, f)
    print(f"wrote {args.out} ({len(result)} cases)")


if __name__ == "__main__":
    sys.exit(main())
