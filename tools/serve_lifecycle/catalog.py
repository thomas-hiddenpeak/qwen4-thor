"""Read-only release identities; explicit stale history is never installable."""
import argparse
import hashlib
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.application_service import stage as shared
from tools.serve_ops.contracts import json_object
from tools.serve_ops.identity import bounded_file

h = shared.h
HEX = re.compile(r'[0-9a-f]{64}\Z')


def pair(binary, sidecar, *, cache=None):
    binary, sidecar = Path(binary).absolute(), Path(sidecar).absolute()
    h.require_regular(binary)
    raw = bounded_file(sidecar, 2*1024**2)
    metadata = json_object(raw)
    for key in ('binary_sha256', 'build_cache_sha256'):
        h.require(isinstance(metadata.get(key), str) and HEX.fullmatch(metadata[key]),
                  'missing or invalid release hash: '+key)
    h.require(isinstance(metadata.get('build_cache'), str) and metadata['build_cache'],
              'missing build cache reference')
    cache = Path(cache or metadata['build_cache']).absolute()
    h.require_regular(cache)
    binary_record, cache_record = h.identity(binary), h.identity(cache)
    h.require(binary_record['sha256'] == metadata['binary_sha256'], 'binary/sidecar mismatch')
    h.require(cache_record['sha256'] == metadata['build_cache_sha256'], 'cache/sidecar mismatch')
    digest = hashlib.sha256(raw).hexdigest()
    h.require(h.sha(sidecar) == digest, 'sidecar changed during observation')
    flag = metadata.get('previous_release_was_stale', False)
    h.require(type(flag) is bool, 'invalid stale-history marker')
    for key in ('previous_binary', 'previous_release'):
        h.require(key not in metadata or isinstance(metadata[key], str) and metadata[key],
                  'invalid history reference')
    protocol = metadata.get('chat_protocol', {})
    h.require(isinstance(protocol, dict), 'invalid protocol metadata')
    observation = protocol.get('observability', {})
    h.require(isinstance(observation, dict), 'invalid observation metadata')
    return dict(binary=binary_record, sidecar=dict(path=str(sidecar), sha256=digest, bytes=len(raw)),
        cache=cache_record, version=metadata.get('version'),
        protocol_sha256=protocol.get('protocol_sha256'),
        observability_version=observation.get('version'),
        previous_binary=metadata.get('previous_binary'), previous_release=metadata.get('previous_release'),
        previous_release_was_stale=flag, pair_identity_verified=True,
        business_acceptance_revalidated=False)


def history(binary, sidecar, *, maximum=64):
    h.require(type(maximum) is int and 1 <= maximum <= 64, 'history traversal limit')
    rows, seen = [], set()
    for _ in range(maximum):
        key = (str(Path(binary).resolve()), str(Path(sidecar).resolve()))
        h.require(key not in seen, 'release history cycle')
        seen.add(key)
        row = pair(binary, sidecar)
        rows.append(row)
        previous = (row['previous_binary'], row['previous_release'])
        if row['previous_release_was_stale']:
            h.require(all(previous), 'stale boundary lacks archival references')
            # These are archival identities only. Do not parse them as a pair or
            # infer a matching legacy cache/acceptance from a stale sidecar.
            for path in previous:h.require_regular(path)
            return dict(state='VERIFIED_TO_LEGACY_BOUNDARY', pairs=rows,
                terminal=dict(kind='explicit_stale_history', installable=False,
                    binary=h.identity(Path(previous[0])), sidecar=h.identity(Path(previous[1]))),
                automatic_restore=False, atomic_snapshot=False)
        if not any(previous):
            return dict(state='VERIFIED_TO_ROOT', pairs=rows, terminal=dict(kind='root'),
                        automatic_restore=False, atomic_snapshot=False)
        h.require(all(previous), 'incomplete release history reference')
        binary, sidecar = previous
    raise RuntimeError('release history exceeds fixed traversal limit')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=ROOT/'build/q4t')
    parser.add_argument('--sidecar', type=Path, default=ROOT/'build/q4t.release.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = dict(state='UNVERIFIED', automatic_restore=False)
    rc = 0
    try:result = history(args.binary, args.sidecar)
    except Exception as error:
        result['error'] = str(error)
        rc = 3
    h.save(args.output, result)
    print(result['state'])
    return rc


if __name__ == '__main__':raise SystemExit(main())
