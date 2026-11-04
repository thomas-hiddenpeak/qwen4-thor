"""Private, immutable diagnostic bundles and integrity-checked local reading."""
import hashlib
import json
import os
from pathlib import Path
import stat

from .contracts import InvalidObservation, VERSION, json_object
from .identity import bounded_file

MARKER = 'q4t.ops.bundle.v1'
MAX_REPORT = 2*1024**2


def encoded(value):
    raw = (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2,
                      allow_nan=False)+'\n').encode()
    if len(raw) > MAX_REPORT:
        raise InvalidObservation('report_size_limit')
    return raw


def write_private(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def write(directory, report):
    directory = Path(directory)
    directory.mkdir(mode=0o700)
    raw = encoded(report)
    write_private(directory/'report.json', raw)
    write_private(directory/'manifest.json', encoded(dict(format=MARKER,
        report_sha256=hashlib.sha256(raw).hexdigest(), report_bytes=len(raw))))
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read(directory):
    directory = Path(directory)
    try:
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) & 0o077):
            raise InvalidObservation('bundle_owner_or_type')
        if {p.name for p in directory.iterdir()} != {'report.json', 'manifest.json'}:
            raise InvalidObservation('bundle_extra_files')
        for name in ('report.json', 'manifest.json'):
            info = (directory/name).lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    stat.S_IMODE(info.st_mode) & 0o077):
                raise InvalidObservation('bundle_owner_or_type')
        manifest = json_object(bounded_file(directory/'manifest.json', 4096))
        raw = bounded_file(directory/'report.json', MAX_REPORT)
        if (manifest.get('format') != MARKER or manifest.get('report_bytes') != len(raw) or
                manifest.get('report_sha256') != hashlib.sha256(raw).hexdigest()):
            raise InvalidObservation('bundle_integrity')
        report = json_object(raw)
        if report.get('version') != VERSION:
            raise InvalidObservation('bundle_version')
        return report
    except OSError:
        raise InvalidObservation('bundle_unavailable') from None
