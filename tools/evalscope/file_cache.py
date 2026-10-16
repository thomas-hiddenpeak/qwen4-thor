"""Non-faulting, per-file Linux cachestat observations (no payload reads).

Cache ownership here means membership in the explicit model file set, not
exclusive process ownership. Existing resident pages count in full. Open/stat
and cachestat do not request the file payload or issue eviction advice.
"""
import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import time


class CacheRange(ctypes.Structure):
    _fields_ = [('off', ctypes.c_uint64), ('length', ctypes.c_uint64)]


class CacheStat(ctypes.Structure):
    _fields_ = [('cached_pages', ctypes.c_uint64),
                ('dirty_pages', ctypes.c_uint64),
                ('writeback_pages', ctypes.c_uint64),
                ('evicted_pages', ctypes.c_uint64),
                ('recently_evicted_pages', ctypes.c_uint64)]


LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.syscall.restype = ctypes.c_long
PAGE_SIZE = os.sysconf('SC_PAGE_SIZE')


def model_files(model_dir):
    """Root model files and ple/mtp assets; exclude hidden download scratch."""
    model_dir = Path(model_dir).resolve()
    paths = []
    for entry in model_dir.iterdir():
        if entry.name.startswith('.'):
            continue
        if entry.is_file():
            paths.append(entry)
        elif entry.name in ('ple', 'mtp') and entry.is_dir():
            paths.extend(path for path in entry.rglob('*')
                         if path.is_file() and not any(
                             part.startswith('.') for part in path.relative_to(entry).parts))
    unique = {}
    for path in sorted(paths):
        stat = path.stat()
        unique.setdefault((stat.st_dev, stat.st_ino), path)
    return list(unique.values())


def inspect_file(path):
    if platform.machine() not in ('aarch64', 'x86_64'):
        raise OSError(errno.ENOSYS, 'cachestat syscall number not verified for architecture')
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        stat = os.fstat(fd)
        result = CacheStat()
        region = CacheRange(0, 0)  # length=0: to EOF, without mapping/touching pages
        ctypes.set_errno(0)
        rc = LIBC.syscall(ctypes.c_long(451), ctypes.c_int(fd),
                          ctypes.byref(region), ctypes.byref(result),
                          ctypes.c_uint(0))
        if rc != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(path))
        fields = {key: getattr(result, key) for key, _ in result._fields_}
        fields.update({'path': str(path), 'dev': stat.st_dev, 'inode': stat.st_ino,
                       'size_bytes': stat.st_size,
                       'resident_bytes': result.cached_pages * PAGE_SIZE})
        return fields
    finally:
        os.close(fd)


def observe_files(paths):
    start = time.time()
    results, errors = [], []
    seen = set()
    for path in paths:
        try:
            result = inspect_file(path)
            identity = (result['dev'], result['inode'])
            if identity not in seen:
                seen.add(identity)
                results.append(result)
        except OSError as error:
            errors.append({'path': str(path), 'errno': error.errno,
                           'error': str(error)})
    known = sum(result['resident_bytes'] for result in results)
    return {'start_t': start, 'end_t': time.time(),
            'method': 'Linux cachestat syscall 451; no payload reads',
            'page_size_bytes': PAGE_SIZE,
            'requested_files': len(paths), 'observed_files': len(results),
            'complete_file_set_observed': not errors,
            'resident_bytes': known if not errors else None,
            'known_resident_lower_bytes': known,
            'files': results, 'errors': errors,
            'scope_note': 'All resident pages in the selected model files, including pre-existing cache; not exclusive process ownership.'}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--model-dir', type=Path)
    ap.add_argument('--file', type=Path, action='append', default=[])
    ap.add_argument('--output', required=True, type=Path)
    args = ap.parse_args()
    if args.output.exists():
        ap.error('refusing to overwrite existing evidence: ' + str(args.output))
    paths = model_files(args.model_dir) if args.model_dir else args.file
    if not paths:
        ap.error('provide --model-dir or at least one --file')
    result = observe_files(paths)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as output_file:
        json.dump(result, output_file, indent=2)
        output_file.write('\n')
    print(json.dumps({key: result[key] for key in
                      ('requested_files', 'observed_files', 'resident_bytes',
                       'start_t', 'end_t')}))
    return 0 if result['complete_file_set_observed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
