"""Restore checksum-bound synthetic fixtures; no model execution or sampling."""
import gzip
import hashlib
import json
from pathlib import Path


def prepare(output):
    directory=Path(__file__).resolve().parent
    archive=directory/'fixtures.json.gz'
    metadata=json.loads((directory/'fixtures.metadata.json').read_text())
    sha=lambda data:hashlib.sha256(data).hexdigest()
    assert sha(archive.read_bytes())==metadata['archive_sha256']
    with gzip.open(archive,'rb') as f:raw=f.read(16*1024*1024+1)
    assert len(raw)==metadata['uncompressed_bytes']<=16*1024*1024
    files=json.loads(raw)['files'];assert set(files)==set(metadata['files'])
    output=Path(output).resolve()
    assert any(p.name in ['build','.q4t-work'] for p in output.parents)
    output.mkdir(parents=True,exist_ok=False)
    for name,text in files.items():
        assert sha(text.encode())==metadata['files'][name]
        path=output/name;assert path.resolve().is_relative_to(output)
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    return output


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();print(prepare(args.output))
