"""Original indexed GIF cases with independent Pillow RGB conversion output."""
from pathlib import Path
import hashlib
import json
import struct
from PIL import Image, __version__

root = Path(__file__).resolve().parent
image = Image.new('P', (64, 32))
palette = [value for i in range(256)
           for value in ((i * 53 + 17) % 256, (i * 31 + 43) % 256,
                         (i * 71 + 89) % 256)]
image.putpalette(palette)
image.putdata([(x // 8 + y // 8) % 8 for y in range(32) for x in range(64)])
image.save(root / 'opaque.gif', optimize=False, interlace=True)
image.save(root / 'transparent.gif', optimize=False, transparency=2,
           comment=b'Original source, transparency color is not black')
image.save(root / 'local.gif', optimize=False, transparency=3,
           include_color_table=True)
# Keep one real LZW image but move it inside a larger logical screen. The
# deliberately different nonzero background tests still RGB canvas semantics.
raw = bytearray((root / 'opaque.gif').read_bytes())
position = 13 + 3 * (2 << (raw[10] & 7))
assert raw[position] == 0x2c
raw[6:10] = struct.pack('<HH', 96, 64)
raw[11] = 4
raw[position+1:position+5] = struct.pack('<HH', 16, 16)
(root / 'offset.gif').write_bytes(raw)
other = image.copy()
other.putdata([(x // 8 + y // 8 + 1) % 8 for y in range(32) for x in range(64)])
image.save(root / 'animated.gif', save_all=True, append_images=[other],
           duration=100, loop=0, optimize=False)
for name in ['opaque', 'transparent', 'local', 'offset']:
    with Image.open(root / (name + '.gif')) as actual:
        assert actual.n_frames == 1
        actual.convert('RGB').save(root / (name + '.png'))
(root / 'identity.json').write_text(json.dumps(dict(producer='Pillow '+__version__,
    source='generate.py', sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
          for p in sorted(root.iterdir()) if p.suffix in ('.gif', '.png')}), indent=2)+'\n')
