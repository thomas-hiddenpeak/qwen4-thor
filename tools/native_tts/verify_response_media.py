"""Independently decode retained Speech SDK response bytes on the validation host."""
import argparse
import array
import hashlib
import io
import json
import subprocess
import wave
from pathlib import Path

TYPES = {
    'sdk': ('application/octet-stream', 'application/octet-stream'),
    'opus': ('audio/opus', 'audio/opus'),
    'wildcard': ('audio/*', 'audio/ogg'),
    'events': ('application/octet-stream', 'text/event-stream'),
}


def decode(path):
    info = subprocess.check_output([
        'ffprobe', '-v', 'error', '-select_streams', 'a:0',
        '-show_entries', 'stream=codec_name,channels', '-of', 'json', path], timeout=8)
    if json.loads(info)['streams'] != [{'codec_name': 'opus', 'channels': 1}]:
        raise ValueError('expected a mono Opus stream')
    # The Jetson FFmpeg build provides the WAV muxer and the signed PCM encoder.
    raw = subprocess.check_output([
        'ffmpeg', '-nostdin', '-v', 'error', '-c:a', 'opus', '-i', path,
        '-t', '25', '-ac', '1', '-ar', '24000', '-c:a', 'pcm_s16le',
        '-f', 'wav', 'pipe:1'], timeout=12)
    with wave.open(io.BytesIO(raw)) as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000):
            raise ValueError('unexpected decoded PCM layout')
        pcm = wav.readframes(25 * 24000)
    samples = array.array('h')
    samples.frombytes(pcm)
    peak = max(map(abs, samples), default=0)
    if not 4800 <= len(samples) < 25 * 24000 or peak <= 200:
        raise ValueError('spoken fixture was silent, too short, or exceeded its bound')
    return dict(name=path.stem, codec='opus', sample_rate=24000, samples=len(samples),
                peak=peak, pcm_sha256=hashlib.sha256(pcm).hexdigest())


def verify(directory):
    receipt = json.loads((directory / 'sdk-result.json').read_bytes())
    rows = receipt['rows']
    if (receipt['negative'] != [406, 400] or [r['name'] for r in rows] != list(TYPES)):
        raise ValueError('incomplete SDK response matrix')
    decoded = []
    for row in rows:
        if (row['accept'], row['mime']) != TYPES[row['name']]:
            raise ValueError('unexpected request or response media type')
        path = directory / (row['name'] + '.opus')
        size = path.stat().st_size
        if not 0 < size == row['bytes'] <= 2 * 1024 * 1024:
            raise ValueError('response byte count differs')
        with path.open('rb') as audio:
            if hashlib.file_digest(audio, 'sha256').hexdigest() != row['sha256']:
                raise ValueError('retained audio differs from the SDK receipt')
        decoded.append(decode(path))
    return decoded


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    print(json.dumps(verify(parser.parse_args().directory), indent=2))
