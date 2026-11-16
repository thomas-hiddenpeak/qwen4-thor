"""Create a simple red-to-blue business clip using explicit software codecs."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]
parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
output = args.output.resolve()
if not any(output.is_relative_to(ROOT / p) for p in ('build', '.q4t-work')):
    parser.error('output must be a new work directory')
output.mkdir(parents=True, exist_ok=False)
colors = [(255, 0, 0)] * 6 + [(0, 0, 255)] * 6
raw = output / 'source.rgb'
raw.write_bytes(b''.join(bytes(color) * (96 * 64) for color in colors))
commands = []
for name, codec, mux in [
    ('transition.mp4', ['openh264enc', 'bitrate=500000', 'gop-size=1',
                        'enable-frame-skip=false', '!', 'h264parse'],
     ['mp4mux', 'faststart=true']),
    ('transition-vp8.webm', ['vp8enc', 'threads=1', 'deadline=1000000',
                            'keyframe-max-dist=1'], ['webmmux']),
    ('transition-vp9.webm', ['vp9enc', 'threads=1', 'deadline=1000000',
                            'keyframe-max-dist=1'], ['webmmux']),
]:
    argv = ['gst-launch-1.0', '-q', 'filesrc', 'location=' + str(raw),
            'blocksize=18432', '!', 'rawvideoparse', 'format=rgb',
            'width=96', 'height=64', 'framerate=2/1', '!', 'videoconvert',
            '!', 'video/x-raw,format=I420', '!', *codec, '!', *mux,
            '!', 'filesink', 'location=' + str(output/name)]
    result = subprocess.run(argv, capture_output=True, timeout=30, check=True)
    commands.append(dict(argv=argv, stdout=result.stdout.decode(),
                         stderr=result.stderr.decode()))
(output/'business-identity.json').write_text(json.dumps(dict(
    source='generate_business.py; original solid red then blue, no labels',
    width=96, height=64, frames=12, fps=2, duration_ns=6000000000,
    colors=colors, gpu=False, commands=commands,
    sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(output.iterdir()) if p.suffix in ('.mp4', '.webm')}
), indent=2) + '\n')
