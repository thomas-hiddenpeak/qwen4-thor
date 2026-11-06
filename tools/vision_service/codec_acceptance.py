#!/usr/bin/env python3
"""One bounded Jetson encode/decode check beneath the shared GPU lease."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.examples.vision_input import sample_video
from tools.mtp_suffix.run_stage import require_guard


def run(args):
    from PIL import Image
    import numpy as np
    require_guard()
    args.output.mkdir(parents=True, exist_ok=False)
    raw = args.output / "motion.rgba"
    with raw.open("xb") as stream:
        for i in range(8):
            stream.write(Image.open(args.fixtures/f"motion-{i:02d}.png").convert("RGBA").tobytes())
    video = args.existing_video or args.output / "motion-h264.mp4"
    command = ["ffmpeg", "-nostdin", "-v", "warning", "-n", "-f", "rawvideo",
        "-pixel_format", "rgba", "-video_size", "384x256", "-framerate", "2",
        "-i", str(raw), "-c:v", "h264_nvenc", "-preset", "p1",
        "-rc", "constqp", "-qp", "18", "-an", str(video)]
    (args.output / "command.json").write_text(json.dumps(command))
    if args.existing_video:
        (args.output / "reused-encoding.json").write_text(json.dumps(dict(
            path=str(video),sha256=hashlib.sha256(video.read_bytes()).hexdigest())))
    else:
        with (args.output / "encode.log").open("xb") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=30)
    part = sample_video(video, frames=8, timeout=45, backend="jetson")
    observations = []
    for i, url in enumerate(part["video_frames"]):
        encoded = base64.b64decode(url.split(",")[1])
        (args.output / f"decoded-{i:02d}.png").write_bytes(encoded)
        actual = np.asarray(Image.open(io.BytesIO(encoded)).convert("RGB"), dtype=np.int16)
        expected = np.asarray(Image.open(args.fixtures/f"motion-{i:02d}.png").convert("RGB"), dtype=np.int16)
        error = float(np.abs(actual-expected).mean())
        red = (actual[:,:,0] > 150) & (actual[:,:,1] < 100) & (actual[:,:,2] < 100)
        ys, xs = np.nonzero(red)
        center = float(xs.mean()) if len(xs) else None
        observations.append(dict(frame=i, mean_abs_uint8=error, center_x=center,
            expected_center_x=35+i*44, passed=bool(error < 8 and center is not None and
                abs(center-(35+i*44)) < 2 and actual.shape == expected.shape)))
    report = dict(passed=all(r["passed"] for r in observations) and
        part["video_timestamps"] == [i*.5 for i in range(8)],
        observations=observations, timestamps=part["video_timestamps"],
        encoder="h264_nvenc", decoder="nvv4l2decoder", resize_convert="nvvidconv",
        inference_gpu_executed=False, performance_acceptance=False,
        video_sha256=hashlib.sha256(video.read_bytes()).hexdigest())
    (args.output / "part.json").write_text(json.dumps(part))
    (args.output / "result.json").write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--existing-video", type=Path)
    raise SystemExit(run(parser.parse_args()))
