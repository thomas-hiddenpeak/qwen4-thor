"""Restore an audited sealed pair using the current GPU guard.

The saved pair and build cache remain authoritative after guard-only updates.
--check is read-only; an actual restore requires a fresh output/lease guard.
"""
import argparse
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools/verify/startup_delivery'))
import deploy as h


def check(package):
    h.verify_seal(package)
    states = []
    for name in ['q4t', 'q4t.release.json']:
        old = h.sha(package / 'previous' / name)
        new = h.sha(package / 'candidate' / name)
        current = h.sha(ROOT / 'build' / name)
        h.require(current in [old, new], 'unknown installed identity: ' + name)
        states.append((name, old, new, current))
    for version in ['previous', 'candidate']:
        h.verify_pair(package / version / 'q4t',
                      package / version / 'q4t.release.json',
                      h.sha(package / version / 'q4t'),
                      package / version / 'CMakeCache.txt')
    return states


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    package = args.package.resolve(strict=True)
    h.require(package.is_relative_to(ROOT / '.q4t-work/audit-p2-20261031'),
              'package outside this audited delivery')
    states = check(package)
    if args.check:
        print('Sealed previous/candidate pairs and installed identities verified.')
        return
    h.require(args.output is not None, 'restore requires --output')
    output = args.output.resolve()
    h.require(output.is_relative_to(ROOT / '.q4t-work'), 'output location')
    h.require_guard(output)
    marker = ROOT / 'build/audit-p2-rollback-recovery.json'
    if marker.exists():
        h.require(h.read(marker)['package'] == str(package), 'foreign recovery')
    else:
        h.save(marker, dict(package=str(package), output=str(output)))
        h.fsync_directory(marker.parent)
    for name, old, new, current in states:
        if current != old:
            h.replace_checked(package / 'previous' / name,
                              ROOT / 'build' / name, new)
    h.verify_pair(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json',
                  h.sha(package / 'previous/q4t'))
    h.save(output / 'restored.json', dict(status='RESTORED',
        package=str(package), binary_sha256=h.sha(ROOT / 'build/q4t'),
        sidecar_sha256=h.sha(ROOT / 'build/q4t.release.json'), finished=time.time()))
    marker.unlink()
    h.fsync_directory(marker.parent)


if __name__ == '__main__':
    main()
