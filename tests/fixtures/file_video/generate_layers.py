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


def paired_intro(source):
    """Repeat the first independent picture across the first two-frame vision group."""
    data = bytearray(source)

    def child(parent, kind):
        match, = [(a, b) for key, a, b in boxes(data, *parent) if key == kind]
        return match

    root = (0, len(data))
    mdat, end = child(root, b"mdat")
    movie = child(root, b"moov")
    tracks = [(a, b) for key, a, b in boxes(data, *movie) if key == b"trak"]
    tables = [child(child(child(track, b"mdia"), b"minf"), b"stbl") for track in tracks]
    sizes, _ = child(tables[0], b"stsz")
    mapping, _ = child(tables[0], b"stsc")
    if (end > movie[0] or len(tables) != 2 or
            struct.unpack_from(">II", data, sizes + 4) != (0, 6) or
            struct.unpack_from(">7I", data, mapping + 4) != (2, 1, 1, 1, 5, 2, 1)):
        raise ValueError("unrecognized original movie sample layout")
    offsets, _ = child(tables[0], b"stco")
    first, second = struct.unpack_from(">II", data, offsets + 8)
    n, m = struct.unpack_from(">II", data, sizes + 12)
    if not mdat <= first < first + n <= second < second + m <= end:
        raise ValueError("invalid first two samples")
    picture = bytes(data[first:first + n])
    growth = n - m
    # The following moov moves with the insertion. Its absolute chunk offsets
    # point into mdat and must account for the changed size of sample two.
    for table in tables:
        start, stop = child(table, b"stco")
        count, = struct.unpack_from(">I", data, start + 4)
        if start + 8 + count * 4 != stop:
            raise ValueError("invalid chunk table")
        for at in range(start + 8, stop, 4):
            offset, = struct.unpack_from(">I", data, at)
            if offset > second:
                struct.pack_into(">I", data, at, offset + growth)
    struct.pack_into(">I", data, sizes + 16, n)
    struct.pack_into(">I", data, mdat - 8, end - mdat + 8 + growth)
    data[second:second + m] = picture
    return bytes(data)


def variants(*, repeat_intro=False):
    source = FIXTURE.read_bytes()
    if repeat_intro:
        source = paired_intro(source)
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
    parser.add_argument("--paired-intro", action="store_true")
    args = parser.parse_args()
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    for name, data in variants(repeat_intro=args.paired_intro):
        (out / (name + ".mp4")).write_bytes(data)
