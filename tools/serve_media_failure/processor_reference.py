"""Compare actual processor bytes with pinned PIL/transformers and patch layout."""
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import PIL
from PIL import Image
import transformers
from transformers.models.qwen2_vl import Qwen2VLImageProcessorPil

ROOT = Path(__file__).resolve().parents[2]


def run(folder):
    assert transformers.__version__ == '5.16.1' and PIL.__version__ == '12.3.0'
    rows = []
    for i, name in enumerate(('a', 'b'), 1):
        image = Image.open(ROOT/f'tools/vision_test_{name}.png').convert('RGB')
        proc = Qwen2VLImageProcessorPil(size={'shortest_edge': 65536, 'longest_edge': 16777216},
            patch_size=16, temporal_patch_size=2, merge_size=2,
            image_mean=[.5]*3, image_std=[.5]*3)
        image_pixels = proc.preprocess(image, return_tensors='np')['pixel_values'].ravel()
        # Two identical frames: these fixtures do not cross the video pixel
        # budget. Independent PIL resize/reshape defines the expected layout.
        w, h = image.size
        rh, rw = round(h/32)*32, round(w/32)*32
        rgb = np.asarray(image.resize((rw, rh), Image.Resampling.BICUBIC))
        norm = (rgb.astype(np.float64)/255).astype(np.float32)
        norm = (norm-np.float32(.5))/np.float32(.5)
        frames = np.stack([norm, norm]).transpose(0, 3, 1, 2)
        # [time,C,h/32,2,16,w/32,2,16] -> block major [hb,wb,mb,mj,C,T,P,P].
        video_pixels = frames.reshape(2, 3, rh//32, 2, 16, rw//32, 2, 16).transpose(
            2, 5, 3, 6, 1, 0, 4, 7).ravel()
        for kind, expected in (('image', image_pixels), ('video', video_pixels)):
            path = folder/f'pixels_{i}_{kind}.bin'
            actual = np.fromfile(path, dtype=np.float32)
            assert actual.shape == expected.shape
            difference = float(np.max(np.abs(actual-expected)))
            assert difference == 0, (name, kind, difference)
            rows.append(dict(name=name, kind=kind, floats=len(actual), max_abs=difference,
                             sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    result = dict(passed=True, transformers=transformers.__version__, pillow=PIL.__version__,
                  cases=rows, video_scope='two identical frames, pinned PIL resize/layout; no motion or torchvision equivalence claim')
    (folder/'reference.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))


if __name__ == '__main__': run(Path(sys.argv[1]))
