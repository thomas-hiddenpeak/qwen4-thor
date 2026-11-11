"""Direct cgroup-v2 capabilities for an explicitly installed q4t service.

No systemd, NVML or process scan is used. Creation changes only a newly named
q4t hierarchy; runtime never migrates another PID. The observer leaf is separate
from the service subtree, so retiring a failed core cannot kill its supervisor.
Memcg limits do not bound all Thor physical/driver allocations.
"""
import ctypes
import fcntl
import os
from pathlib import Path
import stat
import time

from tools.serve_recovery import ledger, store

CGROUP = Path('/sys/fs/cgroup')
MAGIC = 0x63677270
FORMAT = 'q4t-resource-kernel-v1'
NAMES = ('outer', 'service')


def identity(path):
    value = os.stat(path, follow_symlinks=False)
    return [value.st_dev, value.st_ino]


def namespaces(pid='self'):
    result = {}
    for name in ('user', 'cgroup'):
        info = os.stat(f'/proc/{pid}/ns/{name}')
        result[name] = [info.st_dev, info.st_ino]
    return result


def filesystem(fd):
    value = ctypes.create_string_buffer(256)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.fstatfs(fd, value) != 0: raise OSError(ctypes.get_errno(), 'resource filesystem')
    ledger.require(ctypes.c_long.from_buffer(value).value == MAGIC, 'actual cgroup v2 filesystem required')


def directory(path):
    path = Path(path).absolute()
    ledger.require(path.resolve(strict=True) == path and path.is_relative_to(CGROUP),
                   'canonical kernel resource path required')
    for current in (path, *path.parents):
        info = current.lstat()
        ledger.require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and
            not info.st_mode & 0o022, 'resource hierarchy must remain root-owned')
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        filesystem(fd)
        info = os.fstat(fd)
        ledger.require([info.st_dev, info.st_ino] == identity(path), 'resource path changed')
        return fd
    except BaseException: os.close(fd); raise


def read(fd, name, maximum=16384):
    source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        raw = os.read(source, maximum+1)
        ledger.require(len(raw) <= maximum and (not raw or raw.endswith(b'\n')),
                       'bounded kernel resource record required')
        return raw.decode('ascii').strip()
    finally: os.close(source)


def write(fd, name, value):
    target = os.open(name, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        raw = (str(value)+'\n').encode('ascii')
        ledger.require(os.write(target, raw) == len(raw), 'short resource control write')
    finally: os.close(target)


def counters(raw):
    value = {}
    for row in raw.splitlines():
        key, number = row.split()
        ledger.require(key not in value and number.isdecimal() and len(number) <= 20,
                       'invalid resource counter')
        value[key] = int(number)
    return value


def policy(high_mib, max_mib, tasks):
    ledger.require(all(type(x) is int for x in (high_mib, max_mib, tasks)) and
        32 <= high_mib <= max_mib <= 128*1024 and 8 <= tasks <= 4096,
        'explicit resource memory and task bounds required')
    return dict(service=dict(memory_high_bytes=high_mib*1024**2,
        memory_max_bytes=max_mib*1024**2, pids_max=tasks),
        outer=dict(memory_high_bytes=384*1024**2, memory_max_bytes=512*1024**2, pids_max=64),
        swap_max_bytes=0, oom_group=True, physical_ram_limit=False)


def limits(selected, role):
    values = selected[role]
    return {'memory.high': values['memory_high_bytes'], 'memory.max': values['memory_max_bytes'],
            'pids.max': values['pids_max'], 'memory.swap.max': 0, 'memory.oom.group': 1}


def validate(value):
    ledger.keys(value, ('format', 'service_id', 'domain_id', 'runtime_uid', 'boot_id',
                        'root', 'root_identity', 'children', 'namespaces', 'policy'))
    ledger.require(value['format'] == FORMAT and all(ledger.identifier(value[key]) for key in
        ('service_id', 'domain_id', 'boot_id')) and ledger.integer(value['runtime_uid'], minimum=1) and
        value['runtime_uid'] < 2**32-1, 'invalid resource domain identity')
    ledger.require(value['root'] == str(CGROUP/('q4t-resource-'+value['domain_id'])),
                   'resource domain path does not match its identity')
    ledger.keys(value['children'], NAMES); ledger.keys(value['namespaces'], ('user', 'cgroup'))
    for entry in [value['root_identity'], *value['children'].values(), *value['namespaces'].values()]:
        ledger.require(type(entry) is list and len(entry) == 2 and
                       all(ledger.integer(x, minimum=1) for x in entry), 'invalid resource inode')
    chosen = value['policy']; backend = chosen['service']
    ledger.require(chosen == policy(backend['memory_high_bytes']//1024**2,
        backend['memory_max_bytes']//1024**2, backend['pids_max']), 'resource policy drift')
    return value


def create(service_id, domain_id, runtime_uid, selected, *, resume_unpublished=False):
    """Privileged setup; resume is only for a never-published installation."""
    ledger.require(os.geteuid() == 0 and ledger.identifier(service_id) and ledger.identifier(domain_id)
        and ledger.integer(runtime_uid, minimum=1) and runtime_uid < 2**32-1,
        'explicit privileged resource setup required')
    base = directory(CGROUP)
    try:
        ledger.require(os.fstat(base).st_ino == 1 and namespaces() == namespaces(1), 'complete host resource namespace required')
        ledger.require({'memory', 'pids'} <= set(read(base, 'cgroup.subtree_control').split()),
                       'parent must already expose memory and pids controllers')
    finally: os.close(base)
    expected = policy(selected['service']['memory_high_bytes']//1024**2,
                      selected['service']['memory_max_bytes']//1024**2, selected['service']['pids_max'])
    ledger.require(selected == expected, 'invalid selected resource policy')
    root = CGROUP/('q4t-resource-'+domain_id)
    root.mkdir(mode=0o755, exist_ok=resume_unpublished)
    ledger.require(root.lstat().st_uid == 0 and stat.S_IMODE(root.lstat().st_mode) in (0o700, 0o755),
                   'unpublished resource directory changed')
    root.chmod(0o755)
    fd = directory(root)
    try:
        ledger.require(counters(read(fd, 'cgroup.events')).get('populated') == 0 and
            {p.name for p in root.iterdir() if p.is_dir()} <= set(NAMES),
            'unpublished resource hierarchy is not empty')
        for name, amount in (('cgroup.max.depth', 1), ('cgroup.max.descendants', 2)):
            ledger.require(read(fd, name) in ('max', str(amount)), 'unpublished hierarchy limit differs')
            write(fd, name, amount)
        ledger.require(set(read(fd, 'cgroup.subtree_control').split()) <= {'memory', 'pids'},
                       'unpublished controller selection differs')
        write(fd, 'cgroup.subtree_control', '+memory +pids')
        children = {}
        for role in NAMES:
            path = root/role; path.mkdir(mode=0o755, exist_ok=resume_unpublished)
            ledger.require(path.lstat().st_uid == 0 and stat.S_IMODE(path.lstat().st_mode) in (0o700, 0o755),
                           'unpublished resource leaf changed')
            path.chmod(0o755)
            child = directory(path)
            try:
                for name, amount in limits(selected, role).items():
                    default = '0' if name == 'memory.oom.group' else 'max'
                    ledger.require(read(child, name) in (default, str(amount)),
                                   'unpublished resource leaf limit differs')
                    write(child, name, amount)
                children[role] = identity(path)
                # This only grants killing this service's own leaf. Migration,
                # controller limits and hierarchy creation remain root-owned.
                if role == 'service':
                    ledger.require((path/'cgroup.kill').lstat().st_uid in (0, runtime_uid),
                                   'unpublished kill authority changed')
                    os.chown(path/'cgroup.kill', runtime_uid, -1)
            finally: os.close(child)
        return validate(dict(format=FORMAT, service_id=service_id, domain_id=domain_id,
            runtime_uid=runtime_uid, boot_id=store.stamp().boot_id, root=str(root),
            root_identity=identity(root), children=children,
            namespaces=namespaces(), policy=selected))
    finally: os.close(fd)


class Scope:
    def __init__(self, record):
        self.record = validate(record); self.root = Path(record['root']); self.fds = {}
        try:
            self.fds['root'] = directory(self.root)
            for role in NAMES: self.fds[role] = directory(self.root/role)
            self.check()
        except BaseException: self.close(); raise

    def check(self):
        value = self.record
        ledger.require(value['boot_id'] == store.stamp().boot_id and
            value['namespaces'] == namespaces(),
            'resource boot or namespace changed')
        for role, fd in self.fds.items():
            path = self.root if role == 'root' else self.root/role
            expected = value['root_identity'] if role == 'root' else value['children'][role]
            info = os.fstat(fd)
            ledger.require([info.st_dev, info.st_ino] == identity(path) == expected and
                info.st_uid == 0 and not info.st_mode & 0o022 and read(fd, 'cgroup.type') == 'domain',
                'resource kernel domain changed')
        ledger.require(set(self.fds) == {'root', *NAMES} and
            read(self.fds['root'], 'cgroup.max.depth') == '1' and
            read(self.fds['root'], 'cgroup.max.descendants') == '2', 'resource hierarchy boundary changed')
        for role in NAMES:
            for name, expected in limits(value['policy'], role).items():
                ledger.require(read(self.fds[role], name) == str(expected), 'resource controller limit changed')
        return self

    def join_descriptor(self, role):
        """Admin-only capability for moving the eventual child itself to a leaf."""
        ledger.require(os.geteuid() == 0 and role in NAMES, 'privileged self-entry capability required')
        self.check()
        return os.open('cgroup.procs', os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.fds[role])

    def join_self(self, role, fd):
        ledger.require(role in NAMES and fd >= 3, 'invalid resource self-entry')
        self.entry_descriptor(role, fd)
        ledger.require(os.write(fd, b'0\n') == 2, 'short resource self-entry')
        self.member(role)
        return dict(joined=role, device_authorized=False)

    def entry_descriptor(self, role, fd):
        ledger.require(role in NAMES and fd >= 3, 'invalid resource self-entry')
        self.check()
        path = self.root/role/'cgroup.procs'; held = os.fstat(fd)
        ledger.require([held.st_dev, held.st_ino] == identity(path) and
            fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY,
            'resource entry capability does not match selected leaf')

    def member(self, role):
        ledger.require(role in NAMES, 'invalid resource membership')
        self.check()
        lines = Path('/proc/self/cgroup').read_text().splitlines()
        expected = '0::/'+str((self.root/role).relative_to(CGROUP))
        ledger.require(lines == [expected], 'resource self-entry did not take effect')

    def empty(self, role='service'):
        ledger.require(role in NAMES, 'invalid resource leaf')
        self.check()
        events = counters(read(self.fds[role], 'cgroup.events'))
        ledger.require(events.get('populated') in (0, 1), 'resource population unavailable')
        return events['populated'] == 0

    def kill_service(self, *, timeout=5, tick=None):
        """Internal cleanup after lifecycle proof, never a health/error reaction."""
        ledger.require(0 < timeout <= 5, 'bounded resource cleanup required')
        self.check()
        # The original pinned leaf and kernel populated counter cover all its
        # tasks; /proc scans and cached PID lists supply no emptiness authority.
        write(self.fds['service'], 'cgroup.kill', 1)
        deadline = time.monotonic()+timeout
        while not self.empty():
            if time.monotonic() >= deadline: raise TimeoutError('resource subtree remains populated')
            if tick is not None: tick()
            time.sleep(.01)
        return dict(kernel_empty=True, domain_id=self.record['domain_id'],
                    device_authorized=False, physical_ram_limit=False)

    def close(self):
        for fd in self.fds.values(): os.close(fd)
        self.fds.clear()

    def __enter__(self): return self
    def __exit__(self, *_args): self.close()
