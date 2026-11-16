"""Original four-quadrant clips with display transforms; CPU tools only."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
root = args.output.resolve()
root.mkdir(parents=True, exist_ok=False)
width, height = 128, 64
colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
frame = b''.join(bytes(colors[2 * (y >= height // 2) + (x >= width // 2)])
                 for y in range(height) for x in range(width))
raw = root / 'quadrants.rgb'
raw.write_bytes(frame * 4)
commands = []
for name, aspect, codec in [('geometry.mp4', '1/1', 'h264'),
                            ('wide-pixels.mp4', '2/1', 'h264'),
                            ('tall-pixels.mp4', '1/2', 'h264'),
                            ('wide-pixels.webm', '2/1', 'vp8')]:
    encode = ['openh264enc', 'bitrate=500000', 'gop-size=1',
              'enable-frame-skip=false', '!', 'h264parse', '!',
              'mp4mux', 'faststart=true'] if codec == 'h264' else [
                  'vp8enc', 'threads=1', 'deadline=1000000',
                  'keyframe-max-dist=1', '!', 'webmmux']
    argv = ['gst-launch-1.0', '-q', 'filesrc', 'location=' + str(raw),
            'blocksize=' + str(width * height * 3), '!', 'rawvideoparse',
            'format=rgb', 'width=' + str(width), 'height=' + str(height),
            'framerate=2/1', 'pixel-aspect-ratio=' + aspect, '!',
            'videoconvert', '!', 'video/x-raw,format=I420', '!', *encode,
            '!', 'filesink', 'location=' + str(root / name)]
    result = subprocess.run(argv, capture_output=True, check=True, timeout=30)
    commands.append(dict(argv=argv, stdout=result.stdout.decode(),
                         stderr=result.stderr.decode()))


def track_matrix(data):
    def descend(begin, end):
        offset = begin
        while offset < end:
            size, kind = struct.unpack_from('>I4s', data, offset)
            assert 8 <= size <= end - offset
            if kind == b'tkhd':
                version = data[offset + 8]
                assert version in (0, 1)
                return offset + 8 + (40 if version == 0 else 52)
            if kind in (b'moov', b'trak'):
                found = descend(offset + 8, offset + size)
                if found is not None:
                    return found
            offset += size
        assert offset == end
        return None
    found = descend(0, len(data))
    assert found is not None
    return found


# ISO track affine matrix: x'=a*x+c*y+tx; y'=b*x+d*y+ty.
# Corner order is independently specified as TL, TR, BL, BR.
transforms = {
    'rotate-90': (0, 1, -1, 0, height, 0, [2, 0, 3, 1]),
    'rotate-180': (-1, 0, 0, -1, width, height, [3, 2, 1, 0]),
    'rotate-270': (0, -1, 1, 0, 0, width, [1, 3, 0, 2]),
    'mirror-x': (-1, 0, 0, 1, width, 0, [1, 0, 3, 2]),
    'mirror-y': (1, 0, 0, -1, 0, height, [2, 3, 0, 1]),
    'transpose': (0, 1, 1, 0, 0, 0, [0, 2, 1, 3]),
    'anti-transpose': (0, -1, -1, 0, height, width, [3, 1, 2, 0]),
}
for name, (a, b, c, d, tx, ty, _) in transforms.items():
    data = bytearray((root / 'geometry.mp4').read_bytes())
    matrix = [a << 16, b << 16, 0, c << 16, d << 16, 0,
              tx << 16, ty << 16, 1 << 30]
    struct.pack_into('>9i', data, track_matrix(data), *matrix)
    (root / (name + '.mp4')).write_bytes(data)

data = bytearray((root / 'wide-pixels.mp4').read_bytes())
struct.pack_into('>9i', data, track_matrix(data),
                 0, 1 << 16, 0, -(1 << 16), 0, 0,
                 height << 16, 0, 1 << 30)
(root / 'wide-rotate-90.mp4').write_bytes(data)
(root / 'geometry-identity.json').write_text(json.dumps(dict(
    source='generate_geometry.py; original RGB quadrants', width=width,
    height=height, frames=4, fps=2, duration_ns=2000000000, colors=colors,
    transformations=transforms, commands=commands, gpu=False,
    sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.iterdir()) if p.suffix in ('.mp4', '.webm')}
), indent=2) + '\n')
