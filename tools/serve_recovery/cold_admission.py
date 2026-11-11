"""Forward a privileged one-use credential only to the native recovery root.

This launcher validates sealed origin and deployment binding but never consumes
or manufactures admission. The Guardian independently verifies the kernel rule,
complete host scope, its actual cgroup, boot, clock, and still-held GPU lease.
"""
import fcntl
import os
import stat

from tools.serve_recovery import ledger, store

ENVIRONMENT = 'Q4T_DEVICE_ADMISSION_FDS'
NAMES = ('receipt_fd', 'token_fd', 'link_fd', 'program_fd')


def configuration(service_id, manifest_sha256, lease_fd):
    raw = os.environ.get(ENVIRONMENT)
    if raw is None: return None, ()
    numbers = raw.split(',')
    ledger.require(len(numbers) == 4 and all(n.isdecimal() and len(n) <= 7 for n in numbers),
                   'invalid inherited cold device descriptors')
    fds = tuple(int(n) for n in numbers)
    ledger.require(len(set(fds)) == 4 and all(3 <= fd <= 1000000 and fd != lease_fd for fd in fds),
                   'cold device descriptors collide')
    receipt, token, _link, _program = fds
    file, pipe = os.fstat(receipt), os.fstat(token)
    ledger.require(stat.S_ISREG(file.st_mode) and file.st_uid == 0 and
        file.st_nlink == 0 and file.st_mode & 0o077 == 0 and 0 < file.st_size <= 8192 and
        fcntl.fcntl(receipt, fcntl.F_GET_SEALS) == 15 and
        fcntl.fcntl(receipt, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY,
        'cold device receipt must be immutable and root-owned')
    ledger.require(stat.S_ISFIFO(pipe.st_mode) and pipe.st_uid == 0 and
        pipe.st_mode & 0o077 == 0 and
        fcntl.fcntl(token, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY and
        fcntl.fcntl(token, fcntl.F_GETFL) & os.O_NONBLOCK,
        'cold device token must be protected read-only root pipe')
    value = store.decode(os.pread(receipt, 8192, 0))
    ledger.keys(value, ('format', 'claims', 'pipe_device', 'pipe_inode', 'nonce'))
    ledger.require(value['format'] == 'q4t-device-admission-v1' and
        (value['pipe_device'], value['pipe_inode']) == (pipe.st_dev, pipe.st_ino) and
        type(value['claims']) is dict, 'cold device token binding changed')
    claims = value['claims']
    ledger.require(claims.get('service_id') == service_id and
        claims.get('manifest_sha256') == manifest_sha256 and
        claims.get('runtime_uid') == os.geteuid(), 'cold device deployment changed')
    lease = os.fstat(lease_fd)
    ledger.require((claims.get('lease_device'), claims.get('lease_inode')) ==
                   (lease.st_dev, lease.st_ino), 'cold device lease changed')
    # Only the child will inherit explicit duplicates. The launcher does not
    # consume the token or silently discard a requested but invalid admission.
    copies = []
    try:
        for fd in fds: copies.append(fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3))
        config = dict(zip(NAMES, copies)); config['manifest_sha256'] = manifest_sha256
        return config, tuple(copies)
    except BaseException:
        for fd in copies: os.close(fd)
        raise
