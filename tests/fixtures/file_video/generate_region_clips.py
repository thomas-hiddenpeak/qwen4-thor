"""Build a QuickTime movie with two visible diagonal quadrants of the original."""
import argparse
from pathlib import Path
import struct

from generate_rect_clips import append_atom, atom


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    # The two existing short clips in the trailing moov become ignored atoms.
    # The H.264 mdat and each chunk offset remain byte-identical.
    original = Path(__file__).with_name('rect-clips.mov').read_bytes()
    if original.count(b'clip') != 2 or original.index(b'mdat') > original.index(b'moov'):
        raise ValueError('unexpected rectangle fixture')
    base = original.replace(b'clip', b'free')
    end = 0x7fff
    words = (0, 0, 64, end, 32, 0, 128, end, 64, 64, 128, end, end)
    inversions = struct.pack('>' + 'h' * len(words), *words)
    region = struct.pack('>Hhhhh', 10 + len(inversions), 0, 0, 64, 128) + inversions
    output = append_atom(base, [b'moov', b'trak'], atom(b'clip', atom(b'crgn', region)))
    with args.output.open('xb') as stream:
        stream.write(output)


if __name__ == '__main__':
    main()
