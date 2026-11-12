"""Original deterministic WebP inputs; PNGs are real Pillow decoded references."""
from pathlib import Path
import hashlib
import json
import numpy as np
from PIL import Image, __version__, features

root = Path(__file__).resolve().parent
y, x = np.indices((64, 96), dtype=np.uint32)
rgb = np.stack([(x * 7 + y * 3) % 256, (x + y * 13) % 256,
                (x * 3 + y) % 256], axis=-1).astype(np.uint8)
image = Image.fromarray(rgb)
image.save(root / 'lossless.webp', lossless=True, exact=True)
image.save(root / 'lossy.webp', quality=83)
alpha = ((x + y * 5) % 256).astype(np.uint8)
alpha[:, ::16] = 0
Image.fromarray(np.dstack([rgb, alpha])).save(root / 'alpha.webp', quality=87, exact=True)
image.save(root / 'metadata.webp', quality=83, exif=b'Exif\0\0Original fixture',
           icc_profile=b'Original unused color profile metadata')
noise = Image.fromarray(np.random.default_rng(0).integers(0, 256, (64, 128, 3), dtype=np.uint8))
noise.save(root / 'noise.webp', lossless=True, exact=True)
image.save(root / 'animated.webp', save_all=True,
           append_images=[Image.new('RGB', image.size, (12, 34, 56))],
           loop=0, duration=100)
for name in ['lossless', 'lossy', 'alpha', 'metadata', 'noise']:
    with Image.open(root / (name + '.webp')) as actual:
        assert actual.n_frames == 1
        actual.convert('RGB').save(root / (name + '.png'))
(root / 'identity.json').write_text(json.dumps(dict(producer='Pillow '+__version__,
    decoder=features.version('webp'), source='generate.py',
    sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.iterdir()) if p.suffix in ('.webp', '.png')}),indent=2)+'\n')
