"""Bounded local image/video adapter for Q4T's explicit media profile.

No HTTP or GPU activity. Video decoding runs in a disposable CPU worker;
requires opencv-python with FFmpeg. Times are decoded frame PTS, not invented
frame indices. Unsupported/nonmonotonic timestamps fail closed. The server
accepts sampled image frames, never arbitrary files or network URLs.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_PAYLOAD_BYTES = 12 * 1024 * 1024


def image_part(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_PAYLOAD_BYTES * 3 // 4:
        raise ValueError("local image exceeds adapter byte limit")
    with path.open("rb") as stream:
        data = stream.read(MAX_PAYLOAD_BYTES * 3 // 4 + 1)
    if len(data) > MAX_PAYLOAD_BYTES * 3 // 4:
        raise ValueError("image grew beyond adapter limit")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    else:
        raise ValueError("PNG/JPEG input required")
    url = "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")
    return dict(type="image_url", image_url=dict(url=url, detail="auto"))


def sample_video(path, frames=8, timeout=30, backend="cpu"):
    path = Path(path).resolve(strict=True)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INPUT_BYTES:
        raise ValueError("video must be a regular local file at most 128 MiB")
    if type(frames) is not int or not 2 <= frames <= 32:
        raise ValueError("sample count must be 2..32")
    if type(timeout) not in (int, float) or not 0 < timeout <= 60:
        raise ValueError("decode timeout must be positive and at most 60 seconds")
    if backend not in ("cpu", "jetson"):
        raise ValueError("backend must be cpu or jetson; no silent fallback")
    with tempfile.TemporaryDirectory(prefix="q4t-video-") as temporary:
        output = Path(temporary)
        subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()),
                        "--worker", str(path), "--frames", str(frames),
                        "--backend", backend, "--output", str(output)],
                       check=True, timeout=timeout,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       env=dict(os.environ, CUDA_VISIBLE_DEVICES="",
                                OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1"))
        metadata = json.loads((output / "frames.json").read_text())
        urls = [image_part(output / name)["image_url"]["url"]
                for name in metadata["files"]]
        if sum(map(len, urls)) > MAX_PAYLOAD_BYTES:
            raise ValueError("sampled video exceeds 12 MiB payload budget")
        return dict(type="video_frames", video_frames=urls,
                    video_timestamps=metadata["timestamps"])


def worker(path, count, output):
    import cv2
    cv2.setNumThreads(1)
    capture = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG,
        [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_NONE])
    try:
        total = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = capture.get(cv2.CAP_PROP_FPS)
        width = capture.get(cv2.CAP_PROP_FRAME_WIDTH)
        height = capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
        if (not capture.isOpened() or not all(math.isfinite(x) for x in
                (total, fps, width, height)) or not count <= total <= 7200 or
                not 0 < fps <= 120 or total / fps > 600 or
                not 1 <= width <= 4096 or not 1 <= height <= 4096 or
                width * height > 4096 * 2160):
            raise ValueError("unsupported video metadata or adapter limits")
        indices = [round(i * (int(total) - 1) / (count - 1)) for i in range(count)]
        timestamps, files, encoded = [], [], 0
        for i, index in enumerate(indices):
            if not capture.set(cv2.CAP_PROP_POS_FRAMES, index):
                raise ValueError("video seek unsupported")
            ok, frame = capture.read()
            timestamp = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000
            if (not ok or not math.isfinite(timestamp) or timestamp < 0 or
                    timestamp > 86400 or
                    (timestamps and timestamp <= timestamps[-1]) or
                    frame.shape[:2] != (int(height), int(width))):
                raise ValueError("decoded frame geometry/PTS contract failed")
            ok, png = cv2.imencode(".png", frame)
            if not ok:
                raise ValueError("frame PNG encoding failed")
            encoded += (len(png) + 2) // 3 * 4 + 22
            if encoded > MAX_PAYLOAD_BYTES:
                raise ValueError("sampled video exceeds payload limit")
            filename = f"frame-{i:02d}.png"
            (output / filename).write_bytes(png.tobytes())
            files.append(filename)
            timestamps.append(timestamp)
        (output / "frames.json").write_text(json.dumps(
            dict(files=files, timestamps=timestamps)))
    finally:
        capture.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--frames", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=("cpu", "jetson"), default="cpu")
    args = parser.parse_args()
    if args.backend == "cpu":
        worker(args.worker, args.frames, args.output)
    else:
        from jetson_video import decode
        decode(args.worker, args.frames, args.output)
