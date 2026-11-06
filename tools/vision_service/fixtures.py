#!/usr/bin/env python3
"""Create small, deterministic visual business fixtures with external truths.

The natural photograph is generated separately with imagegen and inspected
by a human/agent. Its truth is never inferred from runner output. Other
artifacts are program drawings, including a real lossless short QuickTime video.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw, ImageFont


NATURAL_PROMPT = """Use case: photorealistic-natural. Asset type: a synthetic visual-understanding test photograph for a local software project, not a real event. Primary request: generate one sharp, naturally lit tabletop photograph containing exactly one red ceramic mug on the left, exactly one closed blue hardcover notebook in the center, and exactly one yellow banana on the right. The three objects must be separated, fully visible, with nothing overlapping. Scene/backdrop: plain light oak desk against a neutral pale gray background. Composition/framing: landscape, near frontal slightly elevated view, all objects on the same horizontal row, enough whitespace around the edges. Lighting: soft daylight, realistic shadows and materials. Constraints: no people, no printed text, no letters or numbers, no extra items, no logos or watermark. The intended factual labels are mug=red/left, notebook=blue/center, banana=yellow/right, but actual generated content will be visually inspected before evaluation."""


def build(output):
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "manifest.json"
    if manifest.exists():
        raise ValueError("immutable fixture manifest already exists")
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    font = ImageFont.truetype(str(font_path), 32)
    title = ImageFont.truetype(str(font_path), 44)
    document = Image.new("RGB", (768, 512), "white")
    draw = ImageDraw.Draw(document)
    draw.text((45, 35), "PROJECT STATUS", font=title, fill="black")
    rows = [("Module", "Passed"), ("Vision", "12"), ("Text", "24"),
            ("Tools", "8")]
    for i, (name, value) in enumerate(rows):
        y = 130 + i * 75
        draw.line((45, y + 55, 720, y + 55), fill="gray", width=2)
        draw.text((60, y), name, font=font, fill="black")
        draw.text((470, y), value, font=font, fill="black")
    document.save(output / "document.png")
    document.save(output / "document.jpg", quality=95, subsampling=0)
    chart = Image.new("RGB", (640, 480), "white")
    draw = ImageDraw.Draw(chart)
    draw.text((35, 20), "COMPLETED TASKS", font=font, fill="black")
    for x, label, value, color in ((100, "A", 4, "red"),
                                  (270, "B", 9, "blue"),
                                  (440, "C", 6, "green")):
        y = 400 - 30 * value
        draw.rectangle((x, y, x + 90, 400), fill=color)
        draw.text((x + 30, y - 40), str(value), font=font, fill="black")
        draw.text((x + 30, 415), label, font=font, fill="black")
    chart.save(output / "chart.png")
    # RGB gradient with a tie dimension, for exact decoder/patch-order tests.
    import numpy as np
    y, x = np.mgrid[:272, :336]
    gradient = np.stack((x % 256, y % 256, (3*x + 5*y) % 256), -1).astype("uint8")
    Image.fromarray(gradient).save(output / "gradient-tie.png")
    times = []
    for i in range(8):
        frame = Image.new("RGB", (384, 256), "white")
        draw = ImageDraw.Draw(frame)
        x = 35 + i * 44
        draw.ellipse((x - 18, 110, x + 18, 146), fill="red")
        draw.text((25, 25), "LEFT", font=font, fill="black")
        draw.text((250, 25), "RIGHT", font=font, fill="black")
        frame.save(output / f"motion-{i:02d}.png")
        times.append(i * 0.5)
    video = output / "motion.mov"
    command = ["ffmpeg", "-nostdin", "-v", "error", "-n", "-framerate", "2",
               "-i", str(output / "motion-%02d.png"), "-c:v", "png",
               "-threads", "1", "-pix_fmt", "rgb24", str(video)]
    subprocess.run(command, check=True, timeout=30)
    natural = output / "natural-scene.png"
    if not natural.is_file():
        raise ValueError("copy the separately generated and inspected photograph first")
    data = dict(profile="q4t-vision-fixtures-v1", generated=True,
                font_sha256=hashlib.sha256(font_path.read_bytes()).hexdigest(),
                natural=dict(tool="image_gen.imagegen", prompt=NATURAL_PROMPT,
                    inspected=True, truth=dict(mug="red/left",
                        notebook="blue/center", banana="yellow/right")),
                document=dict(Vision=12, Text=24, Tools=8),
                chart=dict(A=4, B=9, C=6, largest="B"),
                video=dict(direction="left_to_right", object="red circle",
                           source_frames=8, timestamps=times, fps=2,
                           duration_seconds=4, command=command),
                files={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in sorted(output.iterdir()) if p.is_file()})
    with manifest.open("x") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(str(manifest))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    build(parser.parse_args().output)
