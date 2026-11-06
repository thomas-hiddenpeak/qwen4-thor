"""Bounded real-processor checks and lossless same-pixel JPEG/PNG fixture."""
import argparse
import json
from pathlib import Path
import subprocess

from PIL import Image
from tools.vision_service import timing_analysis as a

ROOT = Path(__file__).resolve().parents[2]


def main(probe, output):
    output.mkdir(parents=True)
    prior = ROOT / ".q4t-work/vision-client-20261107/phase-01/fixtures"
    jpeg = prior / "earth.jpg"
    raw = output / "earth.rgb"
    decoded = subprocess.run([str(probe)], input=json.dumps(dict(
        kind="decode", file=str(jpeg), output=str(raw))) + "\n",
        text=True, capture_output=True, timeout=30, check=True)
    geometry = json.loads(decoded.stdout)
    png = output / "earth-same-pixels.png"
    Image.frombytes("RGB", (geometry["width"], geometry["height"]),
                    raw.read_bytes()).save(png)
    cases = [[jpeg], [png], [prior / "cabin.png"],
             [ROOT / "tools/vision_test_a.png"],
             [ROOT / "tools/vision_test_b.png"],
             [ROOT / "tools/vision_test_b.png"] * 2,
             [ROOT / "tools/vision_test_b.png"] * 3,
             [jpeg, png]]
    results = []
    for index, files in enumerate(cases):
        observed = []
        for enabled in (False, True):
            target = output / f"case-{index}-{int(enabled)}.f32"
            body = dict(kind="pixels", files=list(map(str, files)),
                        output=str(target), timing=enabled)
            process = subprocess.run([str(probe)], input=json.dumps(body) + "\n",
                text=True, capture_output=True, timeout=30)
            (output / f"case-{index}-{int(enabled)}.json").write_text(json.dumps(
                dict(request=body, code=process.returncode, stdout=process.stdout,
                     stderr=process.stderr), indent=2))
            a.require(process.returncode == 0, "processor failed")
            rows = a.parse_records(process.stderr)
            if enabled:
                row = rows["processor"]
                jpeg_count = sum(p.suffix == ".jpg" for p in files)
                a.require(row["completed"] == 1 and
                          row["decode_jpeg_calls"] == jpeg_count and
                          row["decode_png_calls"] == len(files) - jpeg_count,
                          "processor codec attribution")
                for stage in ("prepare", "resize", "normalize", "patchify"):
                    a.require(row[stage + "_calls"] == 1, "missing CPU stage")
            else:
                a.require(not rows and not process.stderr, "disabled log")
            observed.append((json.loads(process.stdout), target.read_bytes()))
        a.require(observed[0] == observed[1], "timing changed processor output")
        results.append(dict(files=list(map(str, files)), identical=True,
                            bytes=len(observed[0][1])))
    a.require((output / "case-0-0.f32").read_bytes() ==
              (output / "case-1-0.f32").read_bytes(), "same-pixel fixture differs")
    for label, extra, decode_count in (
            ("cancelled", dict(cancelled=True), 0),
            ("quota", dict(decoder_owned_bytes=1), 1)):
        target = output / (label + ".f32")
        body = dict(kind="pixels", files=[str(jpeg)], output=str(target),
                    timing=True, **extra)
        process = subprocess.run([str(probe)], input=json.dumps(body) + "\n",
                                 text=True, capture_output=True, timeout=30)
        (output / (label + ".json")).write_text(json.dumps(dict(
            request=body, code=process.returncode, stdout=process.stdout,
            stderr=process.stderr), indent=2))
        row = a.parse_records(process.stderr)["processor"]
        a.require(process.returncode == 1 and not target.exists() and
                  row["completed"] == 0 and row["prepare_calls"] == 1 and
                  row["decode_jpeg_calls"] == decode_count and
                  row["patchify_calls"] == 0, "partial CPU timing contract")
    result = dict(passed=True, cases=results, same_pixel_pair=True,
                  incomplete_cases=["cancelled", "quota"])
    (output / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    main(args.probe, args.output)
