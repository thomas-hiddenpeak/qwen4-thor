"""Deterministic, original ZIP fixtures; no downloaded documents or code."""
from io import BytesIO
from pathlib import Path
import stat
import warnings
import zipfile

ROOT = Path(__file__).resolve().parent


def package(entries, compression=zipfile.ZIP_DEFLATED, force_zip64=False):
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = compression
            info.external_attr = mode << 16
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with archive.open(info, "w", force_zip64=force_zip64) as item:
                    item.write(data)
    return stream.getvalue()


def main():
    regular = stat.S_IFREG | 0o600
    entries = [("word/document.xml", "<doc>真实 😀 &amp; text</doc>".encode(), regular),
               ("word/_rels/document.xml.rels", b"<Relationships/>", regular)]
    data = package(entries)
    fixtures = {
        "valid.zip": data,
        "zip64.zip": package(entries, force_zip64=True),
        "truncated.zip": data[:-22],
        "duplicate.zip": package([entries[0], entries[0]]),
        "escape.zip": package([("../secret", b"no", regular)]),
        "symlink.zip": package([("alias", b"/etc/hostname", stat.S_IFLNK | 0o777)]),
        "expanded.zip": package([("large.xml", b"x" * 262144, regular)]),
    }
    stored = bytearray(package(entries, compression=zipfile.ZIP_STORED))
    stored[30 + len(entries[0][0])] ^= 1
    fixtures["crc.zip"] = stored
    encrypted = bytearray(data)
    for signature, flag in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        while (start := encrypted.find(signature, start)) >= 0:
            encrypted[start + flag] |= 1
            start += 4
    fixtures["encrypted.zip"] = encrypted
    for name, content in fixtures.items():
        (ROOT / name).write_bytes(content)


if __name__ == "__main__":
    main()
