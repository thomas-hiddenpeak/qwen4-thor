"""Build independently enabled movie tracks from the two original H.264 streams."""
import argparse
from pathlib import Path
import struct


FIXTURE = Path(__file__).with_name("alternate-tracks.mp4")
CASES = {
    "left-only": ((0,), 0, (0, 0)),
    "right-only": ((1,), 0, (0, 0)),
    "adjacent": ((0, 1), 96, (-2, 3)),
    "first-in-front": ((0, 1), 32, (-2, 3)),
    "second-in-front": ((0, 1), 32, (3, -2)),
}


def boxes(data, start, end):
    while start < end:
        size, kind = struct.unpack_from(">I4s", data, start)
        if not 8 <= size <= end - start:
            raise ValueError("invalid box in the source fixture")
        yield kind, start + 8, start + size
        start += size


def variants():
    source = FIXTURE.read_bytes()
    movie, = [(start, end) for kind, start, end in boxes(source, 0, len(source))
              if kind == b"moov"]
    tracks = []
    for kind, start, end in boxes(source, *movie):
        if kind == b"trak":
            header, = [offset for key, offset, _ in boxes(source, start, end)
                       if key == b"tkhd"]
            tracks.append(header)
    if len(tracks) != 2:
        raise ValueError("expected two video tracks")
    for name, (enabled, x, layers) in CASES.items():
        data = bytearray(source)
        for index, header in enumerate(tracks):
            if data[header] != 0 or struct.unpack_from(">H", data, header + 34)[0]:
                raise ValueError("expected version zero and no alternate group")
            data[header + 3] = (data[header + 3] & ~1) | (index in enabled)
            struct.pack_into(">h", data, header + 32, layers[index])
            # Present the second stream without its original rotation. The
            # translation is in movie pixels and is meaningful for composition.
            struct.pack_into(">9i", data, header + 40, 65536, 0, 0, 0, 65536,
                             0, (x if index else 0) << 16, 0, 1 << 30)
        yield name, bytes(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    out = parser.parse_args().output
    out.mkdir(parents=True, exist_ok=True)
    for name, data in variants():
        (out / (name + ".mp4")).write_bytes(data)
