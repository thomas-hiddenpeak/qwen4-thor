"""Original deterministic RGB grid, solely for file-source/decoder checks."""
from pathlib import Path
import hashlib
import json
from PIL import Image, __version__

root = Path(__file__).resolve().parent
image = Image.new("RGB", (64, 32))
pixels = image.load()
for y in range(32):
    for x in range(64):
        pixels[x, y] = (255, 0, 0) if x < 32 else (0, 255, 0)
image.save(root / "original.png", compress_level=6)
image.save(root / "original.jpg", quality=90, subsampling=0, optimize=False)
(root / "identity.json").write_text(json.dumps({
    "producer": "Pillow " + __version__, "source": "generate.py",
    "width": 64, "height": 32,
    "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
               for p in (root / "original.png", root / "original.jpg")},
}, indent=2) + "\n")
