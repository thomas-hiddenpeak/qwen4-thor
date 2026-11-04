"""Private immutable runner policy; no API keys and no automatic service action."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

MAX_BYTES = 64 * 1024
BOUNDS = dict(max_inflight=(1, 4096), max_control_inflight=(1, 64),
              max_prompt_tokens=(1, 1048576), max_output_tokens=(1, 1048576),
              max_body_bytes=(1024, 16 * 1024**2))


def require(value):
    if not value:
        raise ValueError('invalid private gateway client policy')


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def validate(value):
    require(type(value) is dict and value.keys() ==
            {'version', 'gateway_token', 'clients'})
    require(type(value['version']) is int and value['version'] == 1)
    require(type(value['gateway_token']) is str and
            re.fullmatch('[a-f0-9]{64}', value['gateway_token']))
    clients = value['clients']
    require(type(clients) is list and 1 <= len(clients) <= 64)
    ids = set()
    for client in clients:
        require(type(client) is dict and client.keys() == {'id', *BOUNDS})
        name = client['id']
        require(type(name) is str and re.fullmatch('[a-z][a-z0-9_-]{0,31}', name))
        require(name not in ids)
        ids.add(name)
        for field, (minimum, maximum) in BOUNDS.items():
            require(type(client[field]) is int and minimum <= client[field] <= maximum)
    return value


def parse(raw):
    require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES)
    try:
        return validate(json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                                   parse_constant=lambda _: require(False)))
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('invalid private gateway client policy') from None


def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                info.st_mode & 0o077 == 0 and info.st_nlink == 1 and
                0 < info.st_size <= MAX_BYTES)
        raw = bytearray()
        while len(raw) <= info.st_size:
            part = os.read(fd, info.st_size + 1 - len(raw))
            if not part:
                break
            raw.extend(part)
        after = os.fstat(fd)
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mode', 'st_uid',
                  'st_nlink', 'st_mtime_ns', 'st_ctime_ns')
        require(all(getattr(info, key) == getattr(after, key) for key in fields)
                and len(raw) == info.st_size)
        raw = bytes(raw)
        return raw, parse(raw)
    finally:
        os.close(fd)


def binding(raw):
    value = parse(raw)
    return dict(enabled=True, sha256=hashlib.sha256(raw).hexdigest(),
                clients=len(value['clients']))


def match_clients(gateway, backend):
    validate(backend)
    require({c['id'] for c in gateway['clients']} ==
            {c['id'] for c in backend['clients']})


def observed_matches(observed, expected):
    """Missing discovery is supported only for old, explicitly unscoped ELF."""
    if expected is None:
        require(observed is None or (type(observed) is dict and
                observed.get('enabled') is False and observed.get('sha256') == ''
                and observed.get('clients') == 0))
        return
    require(type(observed) is dict and
            observed.get('profile') == 'q4t-gateway-clients-v1' and
            observed.get('enabled') is True and
            observed.get('mutable') is False and
            observed.get('credential_values_redacted') is True and
            type(observed.get('clients')) is int and
            1 <= observed['clients'] <= 64 and
            all(observed.get(key) == value for key, value in expected.items()))
