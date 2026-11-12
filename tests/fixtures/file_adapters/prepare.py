"""Reproduce fixed upstream document fixtures without photos or previews."""

import argparse
import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile
import io
import gzip
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[3]
BASE = "https://raw.githubusercontent.com/LibreOffice/core/libreoffice-24.2.7.2/"
SOURCES = [
    ("pages.pages", "writerperfect/qa/unit/data/writer/libetonyek/pass/Pages_5.pages",
     "a642451962000128f4f955f1b98b4ac6954dfc0de14ad12080cf650ec2093fe0"),
    ("numbers.numbers", "writerperfect/qa/unit/data/calc/libetonyek/pass/Numbers_2.numbers",
     "b70e6d00d09e94a24c52ecdf9356966728c407311c0ef98f1d56ae0caf17e342"),
    ("keynote.key", "writerperfect/qa/unit/data/impress/libetonyek/pass/Keynote_6.key",
     "6e2fc506c7eeb8a3bbca2892083a61877629f324e40285501f023fa19f95c311"),
    ("sheet.xlsb", "sc/qa/unit/data/xlsb/universal-content.xlsb",
     "3d425c433c4f0688d4d2f2c84bbcfc25494641d0d38cbe96d6a954d820536170"),
    ("keynote-notes.key", "writerperfect/qa/unit/data/impress/libetonyek/pass/Keynote_2.key",
     "ded6dc98bb001690adeeb4884ed77ea90724847b670b98835d72fe3d492c1c0b"),
]


def prepare(output, downloaded=None):
    records = []
    for name, relative, digest in SOURCES:
        if downloaded:
            raw = (downloaded / Path(relative).name).read_bytes()
        else:
            with urllib.request.urlopen(BASE + relative, timeout=20) as response:
                raw = response.read(2 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("upstream source identity changed")
        removed = []
        changes = []
        if name.endswith(".xlsb"):
            data = raw
        else:
            source = zipfile.ZipFile(io.BytesIO(raw))
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as target:
                for item in source.infolist():
                    if name == "keynote-notes.key" and item.filename == "index.apxl.gz":
                        key = "http://developer.apple.com/namespaces/keynote2"
                        sf = "http://developer.apple.com/namespaces/sf"
                        for prefix, uri in [("key", key), ("sf", sf),
                                            ("sfa", "http://developer.apple.com/namespaces/sfa")]:
                            ElementTree.register_namespace(prefix, uri)
                        root = ElementTree.fromstring(gzip.decompress(source.read(item)))
                        paragraph = root.find("{%s}slide-list/{%s}slide/{%s}notes" % (key, key, key)).find(".//{%s}p" % sf)
                        assert paragraph is not None and not list(paragraph) and not paragraph.text
                        paragraph.text = "Original Keynote notes 中文 42"
                        info = zipfile.ZipInfo("index.apxl", (2026, 11, 10, 0, 0, 0))
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = 0o100644 << 16
                        target.writestr(info, ElementTree.tostring(root, encoding="utf-8", xml_declaration=True))
                        changes.append("decompress index.apxl.gz; insert original first-slide notes; store as index.apxl")
                        continue
                    if (item.filename.startswith(("Index/", "Metadata/")) or
                            item.filename in ("Index.zip", "index.xml", "buildVersionHistory.plist")):
                        info = zipfile.ZipInfo(item.filename, (2026, 11, 10, 0, 0, 0))
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = 0o100644 << 16
                        target.writestr(info, source.read(item))
                    else:
                        removed.append(item.filename)
            data = stream.getvalue()
        (output / name).write_bytes(data)
        records.append(dict(filename=name, url=BASE + relative,
                            upstream_sha256=digest, removed=removed, changes=changes,
                            sha256=hashlib.sha256(data).hexdigest()))
    # Preserve the older container's compressed-XML variant with the same
    # original notes, so this is actual content coverage rather than a renamed
    # modern document. No photo/preview member is restored.
    source = zipfile.ZipFile(output / "keynote-notes.key")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as target:
        for item in source.infolist():
            name = item.filename
            data = source.read(item)
            if name == "index.apxl":
                name += ".gz"
                data = gzip.compress(data, mtime=0)
            info = zipfile.ZipInfo(name, (2026, 11, 10, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            target.writestr(info, data)
    data = stream.getvalue()
    (output / "keynote-notes-gzip.key").write_bytes(data)
    original = next(item for item in records if item["filename"] == "keynote-notes.key")
    record = dict(original, filename="keynote-notes-gzip.key",
                  changes=original["changes"] + ["gzip the derived XML with mtime=0 and store as index.apxl.gz"],
                  sha256=hashlib.sha256(data).hexdigest())
    records.append(record)
    (output / "sources.json").write_text(json.dumps(records, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--downloaded", type=Path)
    args = parser.parse_args()
    destination = args.output.resolve()
    if not any(destination.is_relative_to(ROOT / name) for name in (".q4t-work", "build")):
        parser.error("output must be a new directory under .q4t-work/ or build/")
    destination.mkdir(parents=True, exist_ok=False)
    prepare(destination, args.downloaded)
