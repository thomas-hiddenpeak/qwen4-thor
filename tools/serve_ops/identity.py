"""Bind the local listener and executable to a stable process identity."""
import hashlib
import itertools
import os
from pathlib import Path
import re
import stat

from .contracts import HEX, InvalidObservation, integer, json_object


def bounded_file(path, maximum=1048576, *, follow=False):
    flags = os.O_RDONLY | os.O_NONBLOCK | (0 if follow else os.O_NOFOLLOW)
    fd = os.open(path, flags)
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise InvalidObservation('input_not_regular')
        chunks, count = [], 0
        while True:
            part = os.read(fd, min(65536, maximum+1-count))
            if not part:
                return b''.join(chunks)
            chunks.append(part)
            count += len(part)
            if count > maximum:
                raise InvalidObservation('input_size_limit')
    finally:
        os.close(fd)


def release_identity(path):
    try:
        value = json_object(bounded_file(path))
        binary = value['binary_sha256']
        protocol = value['chat_protocol']['protocol_sha256']
        if not HEX.fullmatch(binary) or not HEX.fullmatch(protocol):
            raise InvalidObservation('invalid_release_identity')
        return dict(binary_sha256=binary, protocol_sha256=protocol)
    except (OSError, TypeError, KeyError):
        raise InvalidObservation('release_identity_unavailable') from None


def listener(rows, port, owned):
    matches = []
    for line in rows.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10:
            raise InvalidObservation('invalid_listener_table')
        address, _, endpoint_port = fields[1].partition(':')
        if endpoint_port.upper() != f'{port:04X}' or fields[3] != '0A':
            continue
        if address.upper() not in ('0100007F', '00000000'):
            continue
        matches.append((fields[9], address))
    if len(matches) != 1 or matches[0][0] not in owned:
        raise InvalidObservation('listener_owner_unverified')
    return dict(listener_inode=integer(int(matches[0][0]), 1),
                listener_scope='loopback' if matches[0][1].upper()=='0100007F' else 'wildcard')


def process(pid, port, *, proc=Path('/proc')):
    try:
        integer(pid, 2, 2**31-1)
        directory = proc/str(pid)
        before = bounded_file(directory/'stat', 65536).decode()
        start = integer(int(before.rsplit(')',1)[1].split()[19]), 1)
        boot = bounded_file(proc/'sys/kernel/random/boot_id', 64).decode().strip()
        if not re.fullmatch(r'[0-9a-f-]{36}', boot):
            raise InvalidObservation('boot_identity_unavailable')
        own_net = (proc/'self/ns/net').stat()
        net = (directory/'ns/net').stat()
        if (own_net.st_dev, own_net.st_ino) != (net.st_dev, net.st_ino):
            raise InvalidObservation('network_namespace_mismatch')
        descriptors = list(itertools.islice((directory/'fd').iterdir(), 4097))
        if len(descriptors) > 4096:
            raise InvalidObservation('descriptor_observation_limit')
        owned = set()
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue
            match = re.fullmatch(r'socket:\[([0-9]+)\]', target)
            if match:
                owned.add(match[1])
        selected = listener(bounded_file(proc/'net/tcp', 1048576).decode(), port, owned)
        executable = hashlib.sha256(bounded_file(directory/'exe', 64*1024**2, follow=True)).hexdigest()
        after = bounded_file(directory/'stat', 65536).decode()
        if int(after.rsplit(')',1)[1].split()[19]) != start:
            raise InvalidObservation('process_identity_changed')
        return dict(pid=pid, start_ticks=start, boot_id=boot, executable_sha256=executable,
                    net_namespace_device=net.st_dev, net_namespace_inode=net.st_ino, **selected)
    except InvalidObservation:
        raise
    except (OSError, UnicodeError, ValueError, IndexError):
        raise InvalidObservation('process_identity_unavailable') from None


def same_instance(before, after):
    return before is not None and before == after


def operations(primary, port):
    """Require a distinct loopback socket owned by the exact same process."""
    selected = process(primary['pid'], port)
    fields = ('pid', 'start_ticks', 'boot_id', 'executable_sha256',
              'net_namespace_device', 'net_namespace_inode')
    if (selected['listener_scope'] != 'loopback' or
            selected['listener_inode'] == primary['listener_inode'] or
            any(selected[k] != primary[k] for k in fields)):
        raise InvalidObservation('operations_listener_owner_mismatch')
    return selected
