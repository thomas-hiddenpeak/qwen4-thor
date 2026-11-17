#!/usr/bin/env python3
"""Generate synthetic audio fixtures; no model, GPU or runtime CLI dependency."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import wave


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--only-aac', action='store_true')
    args = parser.parse_args()
    out = args.output.absolute()
    out.mkdir(parents=True, exist_ok=False)

    def wav(name, rate, channels, width, frames):
        with wave.open(str(out / name), 'wb') as file:
            file.setnchannels(channels)
            file.setsampwidth(width)
            file.setframerate(rate)
            file.writeframes(frames)

    mono = [round(.5 * 32767 * math.sin(2 * math.pi * 440 * i / 16000))
            for i in range(16000)]
    wav('mono16.wav', 16000, 1, 2, struct.pack('<16000h', *mono))
    # voaacenc/faad retain codec priming and do not emit the full last partial
    # frame. Explicit tail silence keeps the whole one-second tone in the
    # encoded fixture. It is real input data, never inserted by the decoder.
    wav('aac-input-padded.wav', 16000, 1, 2,
        struct.pack('<16000h', *mono) + bytes(2048 * 2))
    raw = bytearray()
    for i in range(48000):
        phase = 2 * math.pi * i / 48000
        for gain in (.5, .25):
            value = round((gain * math.sin(440 * phase) +
                           .15 * math.sin(11000 * phase)) * 8388607)
            raw.extend(value.to_bytes(3, 'little', signed=True))
    wav('stereo24.wav', 48000, 2, 3, raw)
    for bits in (32, 64):
        data = b''.join(struct.pack('<' + ('f' if bits == 32 else 'd'), value)
            for i in range(1600) for value in ((i % 17 - 8) / 4, (8 - i % 17) / 8))
        fmt = struct.pack('<HHIIHH', 3, 2, 16000, 16000 * 2 * (bits // 8),
                          2 * (bits // 8), bits)
        chunks = b'fmt ' + struct.pack('<I', len(fmt)) + fmt
        chunks += b'data' + struct.pack('<I', len(data)) + data
        (out / f'float{bits}.wav').write_bytes(
            b'RIFF' + struct.pack('<I', len(chunks) + 4) + b'WAVE' + chunks)
    plans = {
        'tone.flac': ['flacenc'],
        'tone.mp3': ['lamemp3enc', '!', 'mpegaudioparse'],
        'tone.m4a': ['voaacenc', '!', 'aacparse', '!', 'mp4mux'],
        'tone.aac': ['voaacenc', '!', 'aacparse', '!', 'audio/mpeg,stream-format=adts'],
        'tone.opus': ['audioresample', '!', 'audio/x-raw,rate=48000', '!',
                      'opusenc', '!', 'oggmux'],
        'tone.ogg': ['vorbisenc', '!', 'oggmux'],
        'tone.webm': ['audioresample', '!', 'audio/x-raw,rate=48000', '!',
                      'opusenc', '!', 'webmmux'],
    }
    if args.only_aac:
        plans = {name: value for name, value in plans.items()
                 if name in ('tone.m4a', 'tone.aac')}
    report = {'status': 'INCOMPLETE', 'commands': [],
              'generator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'gstreamer': subprocess.check_output(
                  ['gst-launch-1.0', '--version'], text=True)}
    # A normal registry scan probes unrelated NVIDIA plugins even for CPU
    # encoding. Give the development CLI only these explicit CPU plugins.
    plugins = out / 'plugins'
    plugins.mkdir()
    system_plugins = Path('/usr/lib/aarch64-linux-gnu/gstreamer-1.0')
    plugin_names = ['coreelements', 'wavparse', 'audioconvert', 'audioresample',
                    'lame', 'audioparsers', 'flac', 'opus', 'ogg', 'vorbis',
                    'voaacenc', 'isomp4', 'matroska']
    for name in plugin_names:
        source = system_plugins / ('libgst' + name + '.so')
        assert source.is_file(), source
        (plugins / source.name).symlink_to(source)
    report['cpu_plugin_sha256'] = {
        name: hashlib.sha256((system_plugins / ('libgst' + name + '.so')).read_bytes()).hexdigest()
        for name in plugin_names}
    env = dict(os.environ, CUDA_VISIBLE_DEVICES='',
               GST_REGISTRY=str(out / 'registry.bin'),
               GST_PLUGIN_SYSTEM_PATH_1_0='', GST_PLUGIN_PATH_1_0=str(plugins),
               GST_REGISTRY_FORK='no',
               XDG_CACHE_HOME=str(out / 'cache'))
    try:
        for name, encoding in plans.items():
            source = 'aac-input-padded.wav' if name in ('tone.m4a', 'tone.aac') else 'mono16.wav'
            command = ['gst-launch-1.0', '-q', '-e', 'filesrc',
                'location=' + str(out / source), '!', 'wavparse', '!',
                'audioconvert', '!', *encoding, '!', 'filesink',
                'location=' + str(out / name)]
            result = subprocess.run(command, env=env, capture_output=True,
                                    text=True, timeout=20)
            report['commands'].append({'command': command, 'returncode': result.returncode,
                                       'stdout': result.stdout, 'stderr': result.stderr})
            assert result.returncode == 0, report['commands'][-1]
        names = ['mono16.wav', 'stereo24.wav', 'float32.wav', 'float64.wav',
                 'aac-input-padded.wav', *plans]
        report['files'] = {name: {'bytes': (out / name).stat().st_size,
            'sha256': hashlib.sha256((out / name).read_bytes()).hexdigest()} for name in names}
        report['status'] = 'GENERATED_NOT_VALIDATED'
    finally:
        (out / 'provenance.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'files': len(report['files'])}))


if __name__ == '__main__':
    main()
