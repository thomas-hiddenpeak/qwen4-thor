"""Plan bounded retention; apply only to verified private diagnostic bundles."""
import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import stat
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_ops import bundle
from tools.serve_ops.contracts import InvalidObservation, integer


def inventory(root):
    root = Path(root)
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
            stat.S_IMODE(info.st_mode) & 0o077):
        raise InvalidObservation('retention_root_not_private')
    entries = list(itertools.islice(root.iterdir(), 10001))
    if len(entries) > 10000:
        raise InvalidObservation('retention_directory_limit')
    records, omitted = [], 0
    for path in entries:
        try:
            report = bundle.read(path)
            created = report['unix_seconds']
            if type(created) not in (int, float) or not 0 <= created <= time.time()+60:
                raise InvalidObservation('bundle_time_invalid')
            inode = path.lstat()
            records.append(dict(name=path.name, created=created,
                device=inode.st_dev, inode=inode.st_ino,
                files={name:dict(sha256=hashlib.sha256((path/name).read_bytes()).hexdigest(),
                    bytes=(path/name).stat().st_size) for name in ('report.json','manifest.json')}))
        except (InvalidObservation, OSError, KeyError):
            omitted += 1
    return records, omitted, dict(device=info.st_dev, inode=info.st_ino)


def plan(root, *, max_age_days=14, max_bundles=100, max_bytes=104857600, now=None):
    integer(max_age_days, 1, 3650)
    integer(max_bundles, 1, 10000)
    integer(max_bytes, 1048576, 2**40)
    now = time.time() if now is None else now
    rows, omitted, identity = inventory(root)
    retained, deleted, total = [], [], 0
    for row in sorted(rows, key=lambda r:(r['created'],r['name']), reverse=True):
        size = sum(f['bytes'] for f in row['files'].values())
        if now-row['created'] > max_age_days*86400 or len(retained) >= max_bundles or total+size > max_bytes:
            deleted.append(row)
        else:
            retained.append(row)
            total += size
    return dict(format='q4t.ops.retention.v1', root_identity=identity,
        policy=dict(max_age_days=max_age_days,max_bundles=max_bundles,max_bytes=max_bytes),
        remove=deleted, retained=len(retained), retained_bytes=total,
        ignored_unowned_or_invalid_entries=omitted, bytes_scope='verified bundle files only',
        destructive=False)


def apply(root, expected):
    root = Path(root)
    rows, _, current_identity = inventory(root)
    if current_identity != expected['root_identity']:
        raise InvalidObservation('retention_root_changed')
    current = {row['name']:row for row in rows}
    # Validate every planned record before any deletion. Unknown entries stay.
    if any(current.get(row['name']) != row for row in expected['remove']):
        raise InvalidObservation('retention_bundle_changed')
    deleted = []
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(root_fd)
        if (opened.st_dev, opened.st_ino) != (current_identity['device'], current_identity['inode']):
            raise InvalidObservation('retention_root_changed')
        for row in expected['remove']:
            if Path(row['name']).name != row['name'] or row['name'] in ('.','..'):
                raise InvalidObservation('retention_invalid_name')
            fd = os.open(row['name'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                info = os.fstat(fd)
                if (info.st_dev,info.st_ino) != (row['device'],row['inode']):
                    raise InvalidObservation('retention_bundle_replaced')
                if set(os.listdir(fd)) != {'report.json', 'manifest.json'}:
                    raise InvalidObservation('retention_bundle_changed')
                for name, expected_file in row['files'].items():
                    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                      dir_fd=fd)
                    try:
                        info = os.fstat(file_fd)
                        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                                info.st_mode & 0o077 or info.st_size != expected_file['bytes']):
                            raise InvalidObservation('retention_bundle_changed')
                        raw = bytearray()
                        while len(raw) <= expected_file['bytes']:
                            part = os.read(file_fd, min(65536, expected_file['bytes']+1-len(raw)))
                            if not part:
                                break
                            raw.extend(part)
                        if hashlib.sha256(raw).hexdigest() != expected_file['sha256']:
                            raise InvalidObservation('retention_bundle_changed')
                    finally:
                        os.close(file_fd)
                for name in ('report.json','manifest.json'):
                    os.unlink(name, dir_fd=fd)
            finally:
                os.close(fd)
            os.rmdir(row['name'], dir_fd=root_fd)
            deleted.append(row['name'])
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    return dict(applied=True, deleted_count=len(deleted), deleted=deleted,
                ignored_entries_untouched=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--max-age-days', type=int, default=14)
    parser.add_argument('--max-bundles', type=int, default=100)
    parser.add_argument('--max-bytes', type=int, default=104857600)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        value = plan(args.directory, max_age_days=args.max_age_days,
                     max_bundles=args.max_bundles, max_bytes=args.max_bytes)
        print(json.dumps(value if not args.apply else apply(args.directory,value), indent=2))
        return 0
    except (InvalidObservation,OSError,ValueError):
        print('retention refused: ownership, integrity or policy validation failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
