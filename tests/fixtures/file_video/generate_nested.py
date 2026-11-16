"""Wrap the original video in deterministic package fixtures; no codec run."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
output = args.output.resolve()
if not any(output.is_relative_to(ROOT / p) for p in ("build", ".q4t-work")):
    parser.error("output must be a new work directory")
output.mkdir(parents=True, exist_ok=False)
spec = importlib.util.spec_from_file_location(
    "nested_fixture", ROOT / "tests/fixtures/file_nested/generate.py")
nested = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nested)
clip = (ROOT / "tests/fixtures/file_video/vp8.webm").read_bytes()
inner = nested.word("Video", [("clip.webm", clip)])
assets = {
    "wrapped-video.docx": nested.word("Wrapper", [("inner.docx", inner)]),
    "two-videos.docx": nested.word(
        "Two videos", [("one.webm", clip), ("two.webm", clip)]),
}
for name, data in assets.items():
    (output / name).write_bytes(data)
(output / "nested-identity.json").write_text(json.dumps({
    "source": "generate_nested.py; synthetic altChunk package boundary cases",
    "video_sha256": hashlib.sha256(clip).hexdigest(),
    "sha256": {name: hashlib.sha256(data).hexdigest()
               for name, data in assets.items()},
    "gpu": False,
}, indent=2) + "\n")
