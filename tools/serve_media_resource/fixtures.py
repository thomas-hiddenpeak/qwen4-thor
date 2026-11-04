"""Generate small real PNG/JPEG fixtures, exclusively in the artifact folder."""
import json
from pathlib import Path
import sys

import PIL
from PIL import Image


def main(folder):
    folder.mkdir()
    rows = []
    for width, height in ((140, 100), (272, 272), (321, 225), (1, 1)):
        image = Image.new('RGB', (width, height), (180, 40, 20))
        for suffix, options in (
                ('png', {}), ('jpg', {}),
                ('progressive.jpg', {'progressive': True})):
            path = folder/f'{width}x{height}.{suffix}'
            image.save(path, **options)
            rows.append(dict(path=str(path), width=width, height=height))
    (folder/'manifest.json').write_text(json.dumps(
        dict(pillow=PIL.__version__, fixtures=rows), indent=2)+'\n')
    print(json.dumps(dict(pillow=PIL.__version__, fixtures=len(rows))))


if __name__ == '__main__':
    main(Path(sys.argv[1]))
