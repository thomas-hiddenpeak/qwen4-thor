"""Generate original six-frame software-encoded clips; no GPU or model access."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
root = args.output.resolve()
root.mkdir(parents=True, exist_ok=False)
colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255),
          (255, 255, 0), (0, 255, 255), (255, 0, 255)]
raw = root / "source.rgb"
raw.write_bytes(b"".join(bytes(color) * (96 * 64) for color in colors))
commands = []
for name, encode, mux in [
    ("h264.mp4", ["openh264enc", "bitrate=500000", "gop-size=1",
                  "enable-frame-skip=false", "!", "h264parse"],
     ["mp4mux", "faststart=true"]),
    ("vp8.webm", ["vp8enc", "threads=1", "deadline=1000000",
                  "keyframe-max-dist=1"], ["webmmux"]),
    ("vp9.webm", ["vp9enc", "threads=1", "deadline=1000000",
                  "keyframe-max-dist=1"], ["webmmux"]),
]:
    argv = ["gst-launch-1.0", "-q", "filesrc", "location=" + str(raw),
            "blocksize=18432", "!", "rawvideoparse", "format=rgb",
            "width=96", "height=64", "framerate=2/1", "!", "videoconvert",
            "!", "video/x-raw,format=I420", "!", *encode, "!", *mux,
            "!", "filesink", "location=" + str(root / name)]
    result = subprocess.run(argv, timeout=30, capture_output=True, check=True)
    commands.append(dict(argv=argv, stderr=result.stderr.decode(),
                         stdout=result.stdout.decode()))
(root / "identity.json").write_text(json.dumps(dict(
    source="generate.py; original solid RGB colors in listed order",
    width=96, height=64, frames=6, fps=2, duration_ns=3000000000,
    colors=colors, gpu=False, commands=commands,
    sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.iterdir()) if p.suffix in (".mp4", ".webm")}
), indent=2) + "\n")
