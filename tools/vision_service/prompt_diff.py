#!/usr/bin/env python3
"""Real tokenizer/preflight vs upstream video expansion; CPU, no model."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import MethodType, SimpleNamespace

import numpy as np
from transformers import AutoTokenizer
from reference_diff import extract


def main(args):
    assert os.environ.get("CUDA_VISIBLE_DEVICES") == ""
    source = args.reference / "models/qwen3_vl/processing_qwen3_vl.py"
    methods = extract(source, {"replace_video_token", "_calculate_timestamps"})
    fake = SimpleNamespace(video_processor=SimpleNamespace(merge_size=2, temporal_patch_size=2),
        video_token="<|video_pad|>", vision_start_token="<|vision_start|>",
        vision_end_token="<|vision_end|>")
    for key in ("replace_video_token", "_calculate_timestamps"):
        setattr(fake, key, MethodType(methods[key], fake))
    tokenizer = AutoTokenizer.from_pretrained(str(args.model), local_files_only=True)
    requests, expected = [], []
    for frames in (2, 3, 8):
        for fps in (2, 24):
            urls = ["data:image/png;base64," + base64.b64encode(
                (args.fixtures / f"motion-{i:02d}.png").read_bytes()).decode() for i in range(frames)]
            part = dict(type="video_frames", video_frames=urls)
            if fps != 24: part["video_timestamps"] = [i/fps for i in range(frames)]
            body = dict(messages=[dict(role="user", content=[dict(type="text", text="Describe."), part])],
                        enable_thinking=False, max_tokens=32)
            requests.append(dict(allow_media=True, max_prefill=8192, max_len=16384,
                body=dict(request=body, include_prompt=True, include_token_ids=True)))
            video = fake.replace_video_token(dict(video_grid_thw=np.array([[(frames+1)//2,16,24]]),
                video_metadata=[SimpleNamespace(fps=fps, frames_indices=list(range(frames)))]), 0)
            plain = tokenizer.apply_chat_template([dict(role="user", content=[dict(type="text", text="Describe."),
                dict(type="video")])], tokenize=False, add_generation_prompt=True, enable_thinking=False)
            expected.append(plain.replace("<|vision_start|><|video_pad|><|vision_end|>", video))
    result = subprocess.run([str(args.probe), str(args.model / "tokenizer.json")],
        input="".join(json.dumps(x)+"\n" for x in requests), text=True,
        capture_output=True, timeout=60)
    args.output.with_suffix(".stderr").write_text(result.stderr)
    args.output.with_suffix(".stdout").write_text(result.stdout)
    result.check_returncode()
    actual = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(actual) == len(expected)
    observations = []
    for index, (observed, reference) in enumerate(zip(actual, expected)):
        tokens = tokenizer.encode(reference, add_special_tokens=False)
        passed = observed.get("token_ids") == tokens and observed.get("prompt_tokens") == len(tokens)
        observations.append(dict(index=index, passed=passed, expected_tokens=tokens,
            expected_prompt=reference, actual=observed))
    report = dict(passed=all(x["passed"] for x in observations),
        reference_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        probe_sha256=hashlib.sha256(args.probe.read_bytes()).hexdigest(),
        assumed_attention_shared_bytes=48128, gpu_query=False, observations=observations)
    with args.output.open("x") as out: json.dump(report, out, indent=2)
    print(json.dumps(dict(cases=len(observations), passed=report["passed"])))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("reference", "probe", "model", "fixtures", "output"):
        parser.add_argument("--"+flag, required=True, type=Path)
    raise SystemExit(main(parser.parse_args()))
