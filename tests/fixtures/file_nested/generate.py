"""Original deterministic mixed/nested documents; no external codec or model."""
import argparse
import io
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[3]
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE = "http://schemas.openxmlformats.org/package/2006/relationships"
WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def package(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, (2026, 11, 10, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, data)
    return output.getvalue()


def pdf():
    stream = b"0 1 0 rg 0 0 200 100 re f\n0 0 0 rg BT /F1 12 Tf 10 50 Td (Embedded actual PDF 42) Tj ET\n"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream"]
    data = b"%PDF-1.7\n"; offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    for offset in offsets: data += f"{offset:010d} 00000 n \n".encode()
    return data + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def word(label, parts):
    entries = {"[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
               "_rels/.rels": f'<Relationships xmlns="{PACKAGE}"><Relationship Id="main" Type="{REL}/officeDocument" Target="word/document.xml"/></Relationships>'}
    body = f"<w:p><w:r><w:t>{label} before</w:t></w:r></w:p>"
    relations = []
    for index, (name, data) in enumerate(parts):
        body += f'<w:altChunk r:id="part{index}"/><w:p><w:r><w:t>{label} after {index}</w:t></w:r></w:p>'
        relations.append(f'<Relationship Id="part{index}" Type="{REL}/aFChunk" Target="{name}"/>')
        entries["word/" + name] = data
    entries["word/document.xml"] = f'<w:document xmlns:w="{WORD}" xmlns:r="{REL}"><w:body>{body}</w:body></w:document>'
    entries["word/_rels/document.xml.rels"] = f'<Relationships xmlns="{PACKAGE}">' + ''.join(relations) + '</Relationships>'
    return package(entries)


def generate(output):
    legacy = (ROOT / "tests/fixtures/file_legacy/word.doc").read_bytes()
    page = pdf()
    mixed = word("Outer", [("legacy.doc", legacy), ("page.pdf", page)])
    documents = {"mixed.docx": mixed, "wrapped.docx": word("Wrapper", [("nested.docx", mixed)]),
                 "two-pdf.docx": word("TwoPDF", [("one.pdf", page), ("two.pdf", page)]),
                 "bad-child.docx": word("Partial", [("good.doc", legacy), ("bad.pdf", b"%PDF-corrupt")]),
                 "count.docx": word("Count", [(f"part{i}.doc", legacy) for i in range(3)]),
                 "page.pdf": page}
    for name, data in documents.items(): (output / name).write_bytes(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(); output = args.output.resolve()
    if not any(output.is_relative_to(ROOT / part) for part in ("build", ".q4t-work")):
        parser.error("output must be a new work directory")
    output.mkdir(parents=True, exist_ok=False)
    generate(output)
