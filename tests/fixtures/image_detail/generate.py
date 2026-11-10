"""Original pixel fixture and independent Pillow/NumPy preprocessing oracle.

Dimensions are explicit policy examples, not copied from the C++ planner.
No model, GPU, network or third-party image content is used.
"""
from pathlib import Path
import hashlib
import json
import numpy as np
from PIL import Image, __version__

root = Path(__file__).resolve().parent
y, x = np.indices((1153, 2305), dtype=np.uint32)
rgb = np.stack([(x * 3 + y) % 256, (x + y * 5) % 256,
                (x // 7 + y // 3) % 256], axis=-1).astype(np.uint8)
image = Image.fromarray(rgb)
image.save(root / "pattern.png", compress_level=6)
result = {"producer": {"pillow": __version__, "numpy": np.__version__},
          "source": "generate.py", "source_width": 2305, "source_height": 1153,
          "source_sha256": hashlib.sha256((root / 'pattern.png').read_bytes()).hexdigest(),
          "modes": {}}
for mode, width, height in [('auto', 2304, 1152), ('low', 512, 256),
                            ('high', 2048, 1024), ('original', 2336, 1184)]:
    pixels = (np.pad(rgb, ((0, height - 1153), (0, width - 2305), (0, 0)),
                     mode='edge') if mode == 'original' else
              np.asarray(image.resize((width, height), Image.Resampling.BICUBIC)))
    normalized = (pixels.astype(np.float64) * (1.0 / 255.0)).astype(np.float32)
    normalized = (normalized - np.float32(0.5)) / np.float32(0.5)
    patches = normalized.reshape(height // 32, 2, 16, width // 32, 2, 16, 3)
    patches = patches.transpose(0, 3, 1, 4, 6, 2, 5).reshape(-1, 3, 16, 16)
    patches = np.repeat(patches[:, :, None, :, :], 2, axis=2).astype('<f4')
    result['modes'][mode] = dict(width=width, height=height,
        elements=patches.size, sha256=hashlib.sha256(patches.tobytes()).hexdigest())
(root / 'reference.json').write_text(json.dumps(result, indent=2) + '\n')
