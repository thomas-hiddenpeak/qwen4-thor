#!/usr/bin/env python3
"""Compare real host decoding/resize/patchification with fixed HF CPU classes."""
import argparse
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image
import torch
from transformers.models.qwen2_vl import Qwen2VLImageProcessorPil
from transformers.models.qwen3_vl.video_processing_qwen3_vl import Qwen3VLVideoProcessor


def main(args):
    assert os.environ.get("CUDA_VISIBLE_DEVICES") == ""
    torch.set_num_threads(1)
    args.output.mkdir(parents=True, exist_ok=False)
    image = Qwen2VLImageProcessorPil(size=dict(shortest_edge=65536,
        longest_edge=16777216), patch_size=16, temporal_patch_size=2,
        merge_size=2, image_mean=[.5]*3, image_std=[.5]*3)
    video = Qwen3VLVideoProcessor(size=dict(shortest_edge=4096,
        longest_edge=25165824), patch_size=16, temporal_patch_size=2,
        merge_size=2, image_mean=[.5]*3, image_std=[.5]*3)
    cases = [[name] for name in ("gradient-tie.png", "document.png",
              "document.jpg", "natural-scene.png")]
    cases += [[f"motion-{i:02d}.png" for i in range(n)] for n in (2, 3, 8)]
    cases += [["gradient-tie.png"] * 5]
    observations = []
    for i, names in enumerate(cases):
        files = [args.fixtures / name for name in names]
        images = [Image.open(path).convert("RGB") for path in files]
        if len(names) == 1:
            result = image.preprocess(images, return_tensors="np")
            expected = result["pixel_values"]
            grid = result["image_grid_thw"][0].tolist()
            max_limit, rel_limit = 1e-6, 1e-6
        else:
            result = video(videos=[images], do_sample_frames=False,
                           return_tensors="np", device="cpu")
            expected = result["pixel_values_videos"]
            grid = result["video_grid_thw"][0].tolist()
            # Existing explicit Pillow-vs-torchvision backend contract.
            max_limit, rel_limit = .02, .005
        binary = args.output / f"case-{i}.f32"
        request = dict(kind="pixels", files=list(map(str, files)), output=str(binary))
        process = subprocess.run([str(args.probe)], input=json.dumps(request)+"\n",
                                 text=True, capture_output=True, timeout=60, check=True)
        actual_grid = json.loads(process.stdout)["grid"]
        actual = np.fromfile(binary, dtype=np.float32).reshape(expected.shape)
        difference = actual.astype(np.float64) - expected.astype(np.float64)
        absolute = float(np.abs(difference).max())
        relative = float(np.linalg.norm(difference) / max(1e-12, np.linalg.norm(expected)))
        reference_path = args.output / f"case-{i}.reference.f32"
        np.asarray(expected, dtype=np.float32).tofile(reference_path)
        observations.append(dict(files=names, expected_grid=grid, actual_grid=actual_grid,
            values=actual.size, max_abs=absolute, relative_l2=relative,
            max_abs_limit=max_limit, relative_l2_limit=rel_limit,
            passed=grid == actual_grid and absolute <= max_limit and relative <= rel_limit))
    sources = [Path(inspect.getfile(cls)) for cls in
               (Qwen2VLImageProcessorPil, Qwen3VLVideoProcessor)]
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    report = dict(device="cpu", references=hashes, observations=observations,
                  passed=all(item["passed"] for item in observations))
    (args.output / "result.json").write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("fixtures", "probe", "output"):
        parser.add_argument("--"+flag, required=True, type=Path)
    raise SystemExit(main(parser.parse_args()))
