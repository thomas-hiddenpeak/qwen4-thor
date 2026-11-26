"""Rebuild the single-track rectangle fixture from the original four quadrants."""
import argparse
from pathlib import Path
import struct
import subprocess
import tempfile


def atom(kind, data):
    return struct.pack(">I4s", len(data) + 8, kind) + data


def append_atom(data, path, item):
    if not path:
        return data + item
    result = bytearray()
    offset = 0
    matches = 0
    while offset < len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        if not 8 <= size <= len(data) - offset:
            raise ValueError("invalid box in quadrants fixture")
        content = data[offset + 8:offset + size]
        if kind == path[0]:
            content = append_atom(content, path[1:], item)
            matches += 1
        result.extend(atom(kind, content))
        offset += size
    if matches != 1:
        raise ValueError("expected exactly one " + repr(path[0]))
    return bytes(result)


def rect(top, left, bottom, right):
    return atom(b"clip", atom(b"crgn", struct.pack(">Hhhhh", 10, top, left,
                                                 bottom, right)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        mov = Path(directory) / "base.mov"
        original = Path(__file__).with_name("geometry.mp4")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", original,
                        "-map", "0:v:0", "-c", "copy", "-map_metadata", "-1",
                        "-f", "mov", mov], check=True)
        content = mov.read_bytes()
    # moov follows mdat in this stream copy; adding metadata leaves every
    # H.264 chunk at its original absolute file offset.
    if content.find(b"mdat") >= content.find(b"moov"):
        raise ValueError("expected trailing QuickTime movie header")
    content = append_atom(content, [b"moov"], rect(2, 3, 10, 20))
    content = append_atom(content, [b"moov", b"trak"], rect(0, 5, 8, 16))
    with args.output.open("xb") as stream:
        stream.write(content)


if __name__ == "__main__":
    main()
