#!/usr/bin/env python3
"""CPU-only production geometry/MRoPE differential against fixed HF source.

Extracts the original function ASTs, without instantiating a model. Records
source and probe hashes and all observations, including failures. Run with
the reference Python containing CPU torch, and CUDA_VISIBLE_DEVICES=''.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import subprocess
from types import MethodType, SimpleNamespace

import torch


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract(path, names):
    tree = ast.parse(path.read_text())
    selected = [n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(selected) == len(names)
    for function in selected:
        function.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[
        ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    ns = dict(torch=torch, itertools=itertools, math=math)
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), ns)
    return ns


def run(args):
    assert os.environ.get("CUDA_VISIBLE_DEVICES") == "", "CPU-only reference"
    torch.set_num_threads(1)
    source = args.reference / "models"
    paths = [source / "qwen2_vl/image_processing_pil_qwen2_vl.py",
             source / "qwen3_vl/video_processing_qwen3_vl.py",
             source / "qwen4_exp/modeling_qwen4_exp.py"]
    image_resize, video_resize = [extract(p, {"smart_resize"})["smart_resize"]
                                  for p in paths[:2]]
    methods = extract(paths[2], {"get_vision_position_ids", "get_rope_index"})
    model = SimpleNamespace(config=SimpleNamespace(
        vision_config=SimpleNamespace(spatial_merge_size=2)))
    for name in ("get_vision_position_ids", "get_rope_index"):
        setattr(model, name, MethodType(methods[name], model))
    requests, expected = [], []
    # Includes nearest-even ties, odd-frame budget boundaries, and tiny input.
    shapes = [(1, 1), (32, 32), (64, 96), (100, 140), (256, 256),
              (272, 272), (304, 336), (400, 800), (720, 1280),
              (1080, 1920), (2000, 3200), (4096, 4096)]
    for height, width in shapes:
        for frames in (1, 2, 3, 5, 7, 17, 32):
            requests.append(dict(kind="geometry", height=height, width=width,
                                 frames=frames))
            if frames == 1:
                h, w = image_resize(height, width, 32, 65536, 16777216)
            else:
                h, w = video_resize(frames, height, width, 2, 32, 4096,
                                    25165824)
            expected.append(dict(ok=True, height=h, width=w,
                                 grid=[(frames + 1) // 2, h // 16, w // 16]))
    # Actual reference distinguishes modalities using mm_token_type_ids.
    # Per-block separators prevent collapsing distinct same-modality grids.
    for blocks in ([], [(1, 2, 4)], [(1, 8, 4)], [(2, 4, 6)],
                   [(1, 4, 8), (2, 2, 6), (2, 2, 6), (1, 2, 2)],
                   [(2, 4, 6)] * 16):
        for prefix, suffix in ((0, 0), (3, 0), (1, 5)):
            if not blocks and not prefix and not suffix:
                continue
            ids, modalities, grids, images, videos = [], [], [], [], []
            ids += [4] * prefix
            modalities += [0] * prefix
            for i, (mod, h, w) in enumerate(blocks):
                if i:
                    ids += [3, 4, 5]
                    modalities += [0] * 3
                ids += [10 if mod == 1 else 11] * (h * w // 4)
                modalities += [mod] * (h * w // 4)
                grids.append([1, h, w])
                (images if mod == 1 else videos).append([1, h, w])
            ids += [4] * suffix
            modalities += [0] * suffix
            pos, delta = model.get_rope_index(
                torch.tensor([ids]), torch.tensor([modalities]),
                torch.tensor(images) if images else None,
                torch.tensor(videos) if videos else None)
            requests.append(dict(kind="positions", ids=ids, grids=grids))
            expected.append(dict(ok=True, positions=pos.flatten().tolist(),
                                 delta=delta.item()))
    result = subprocess.run([str(args.probe)],
                            input="".join(json.dumps(x) + "\n" for x in requests),
                            text=True, capture_output=True, timeout=60)
    actual = [json.loads(line) for line in result.stdout.splitlines()]
    assert result.returncode == 0 and len(actual) == len(requests), result.stderr
    observations = [dict(request=q, expected=e, actual=a, passed=e == a)
                    for q, e, a in zip(requests, expected, actual)]
    report = dict(profile="vision-reference-v1", device="cpu",
                  references={str(p): sha(p) for p in paths},
                  probe_sha256=sha(args.probe), observations=observations,
                  passed=all(x["passed"] for x in observations))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as file:
        json.dump(report, file, indent=2)
        file.write("\n")
    failures = [x for x in observations if not x["passed"]]
    print(json.dumps(dict(cases=len(observations), failures=len(failures),
                          first_failures=failures[:3])))
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(run(parser.parse_args()))
