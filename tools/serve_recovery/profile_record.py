"""Bounded qualification I/O; ordinary service metadata keeps its 2 MiB limit."""
import json
import os
from pathlib import Path

from tools.serve_recovery import ledger
from tools.serve_service import package

MAX_BYTES = 8 << 20
LEGACY_BYTES = 2 << 20


def shape(value, size):
    ledger.require(size <= MAX_BYTES, 'profile evidence size limit')
    if size > LEGACY_BYTES:
        ledger.require(type(value) is dict and value.get('format') ==
            'q4t-profile-business-v18' and type(value.get('reused_business')) is dict and
            value['reused_business'].get('format') in (
                'q4t-prompt-profile-source-reuse-v1',
                'q4t-prompt-refusal-source-reuse-v1',
                'q4t-prompt-refusal-source-assembly-v1'),
            'only explicit Prompt source records have the larger evidence bound')


def read(path):
    package.regular(path)
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    ledger.require(len(raw) <= MAX_BYTES, 'profile evidence size limit')

    def unique(pairs):
        result = {}
        for key, item in pairs:
            ledger.require(key not in result, 'duplicate profile evidence key')
            result[key] = item
        return result

    value = json.loads(raw, object_pairs_hook=unique,
        parse_constant=lambda _: ledger.require(False, 'non-finite profile evidence'))
    shape(value, len(raw))
    return value


def save(path, value):
    path = Path(path)
    raw = ledger.canonical(value) + b'\n'
    shape(value, len(raw))
    temporary = path.with_name(path.name + '.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    ledger.require(read(path) == value, 'profile evidence did not round trip')
