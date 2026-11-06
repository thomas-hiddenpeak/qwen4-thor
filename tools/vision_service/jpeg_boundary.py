#!/usr/bin/env python3
"""Targeted JPEG failure isolation; retain PNG exact and video limits unchanged.

JPEG's decoder is stb_image in production and libjpeg in Pillow. Require the
production pipeline to equal processing the production-decoded PNG exactly.
The independent source comparison records all pixel differences. For these
fixtures only, bound source error to one uint8 step, then record HTTP quality
separately; this is not a universal JPEG equivalence or precision claim.
"""
import argparse
import json
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image


def main(args):
    args.output.mkdir(parents=True, exist_ok=False)
    def probe(value):
        result = subprocess.run([str(args.probe)], input=json.dumps(value)+"\n",
                                capture_output=True, text=True, check=True, timeout=30)
        return json.loads(result.stdout)
    raw = args.output / "stb.rgb"
    shape = probe(dict(kind="decode", file=str(args.jpeg), output=str(raw)))
    actual = np.fromfile(raw, dtype=np.uint8).reshape(shape["height"], shape["width"], 3)
    expected = np.array(Image.open(args.jpeg).convert("RGB"))
    difference = np.abs(actual.astype(int) - expected.astype(int))
    decoded = args.output / "stb-decoded.png"
    Image.fromarray(actual).save(decoded)
    outputs = []
    for name, path in (("original", args.jpeg), ("decoded", decoded)):
        target = args.output / (name + ".f32")
        probe(dict(kind="pixels", files=[str(path)], output=str(target)))
        outputs.append(target.read_bytes())
    report = dict(source_max_uint8=int(difference.max()),
        different_channels=int(np.count_nonzero(difference)), channels=actual.size,
        downstream_exact=outputs[0] == outputs[1], source_bound_uint8=1,
        jpeg_pillow_bit_exact=False, runtime_changes=False,
        passed=bool(difference.max() <= 1 and outputs[0] == outputs[1]))
    (args.output / "result.json").write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("probe", "jpeg", "output"):
        parser.add_argument("--"+flag, required=True, type=Path)
    raise SystemExit(main(parser.parse_args()))
