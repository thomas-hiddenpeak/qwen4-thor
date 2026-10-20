"""Restore the pre-hardening default pair; run under a fresh GPU lease."""
import json
from deploy_accepted import ROOT, OUT, OLD, NEW, sha, replace


def main():
    binary = ROOT / 'build/q4t'
    sidecar = ROOT / 'build/q4t.release.json'
    previous = OUT / 'previous'
    expected = json.loads((OUT / 'rollback-identity.json').read_text())
    if sha(binary) != NEW:
        raise ValueError('refuse to replace an unexpected default')
    for name, digest in expected.items():
        if sha(previous / name) != digest:
            raise ValueError('backup changed: ' + name)
    metadata = json.loads((previous / 'q4t.release.json').read_text())
    if metadata['binary_sha256'] != OLD or sha(previous / 'q4t') != OLD:
        raise ValueError('backup identity mismatch')
    if sha(__import__('pathlib').Path(metadata['build_cache'])) != metadata['build_cache_sha256']:
        raise ValueError('backup build-cache changed')
    replace(previous / 'q4t', binary)
    replace(previous / 'q4t.release.json', sidecar)
    print(json.dumps({'status': 'ROLLED_BACK', 'binary_sha256': sha(binary)}))


if __name__ == '__main__':
    main()
