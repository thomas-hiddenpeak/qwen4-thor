"""Owned delegated backend cgroups. Memcg is not total Thor physical RAM."""
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import time

from tools.serve_service import package as pkg

CGROUP = Path('/sys/fs/cgroup')
PROFILE = 'q4t-backend-host-envelope-v1'
PROPERTIES = ('Id', 'LoadState', 'ActiveState', 'SubState', 'Description',
              'ControlGroup', 'InvocationID', 'Delegate', 'DelegateControllers',
              'OOMPolicy')
FIELDS = ('memory.current', 'memory.peak', 'memory.swap.current',
          'memory.events', 'pids.current', 'pids.events', 'cgroup.events')


def policy(high_mib, max_mib, tasks):
    pkg.require(all(type(v) is int for v in (high_mib, max_mib, tasks)) and
        32 <= high_mib <= max_mib <= 128*1024 and 4 <= tasks <= 4096,
        'explicit host high/max MiB and task limit required')
    return dict(profile=PROFILE, memory_high_bytes=high_mib*1024**2,
        memory_max_bytes=max_mib*1024**2, swap_max_bytes=0, tasks_max=tasks,
        control_max_bytes=512*1024**2, control_tasks_max=64,
        oom_group=True, physical_ram_limit=False)


def validate(value):
    pkg.require(type(value) is dict, 'resource policy object required')
    expected = policy(value['memory_high_bytes']//1024**2,
                      value['memory_max_bytes']//1024**2, value['tasks_max'])
    pkg.require(value == expected and all(type(value[k]) is type(expected[k])
        for k in expected), 'unknown resource policy')
    return value


def dependencies():
    return {name: dict(path=str(Path('/usr/bin', name).resolve()),
            sha256=pkg.sha(Path('/usr/bin', name).resolve()))
            for name in ('busctl', 'systemctl')}


def verify_dependencies(value):
    pkg.require(set(value) == {'busctl', 'systemctl'}, 'resource dependencies')
    for name, row in value.items():
        pkg.require(set(row) == {'path', 'sha256'} and
            Path(row['path']) == Path('/usr/bin', name).resolve() and
            pkg.sha(row['path']) == row['sha256'], 'resource manager changed')


def environment():
    path = Path('/run/user') / str(os.getuid())
    info = path.lstat(); bus = (path/'bus').lstat()
    pkg.require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and
        stat.S_IMODE(info.st_mode) == 0o700 and stat.S_ISSOCK(bus.st_mode) and
        bus.st_uid == os.getuid(), 'private user manager unavailable')
    return dict(PATH='/usr/bin:/bin', LANG='C.UTF-8',
                XDG_RUNTIME_DIR=str(path), DBUS_SESSION_BUS_ADDRESS='unix:path='+str(path/'bus'))


def command(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=5,
                            env=environment())
    pkg.require(len(result.stdout)+len(result.stderr) <= 65536,
                'manager response bound')
    return result


def properties(unit):
    pkg.require(re.fullmatch(r'q4t-host-[0-9a-f]{32}\.scope', unit), 'owned resource unit name')
    result = command(['/usr/bin/systemctl', '--user', 'show', unit,
                      *('--property='+key for key in PROPERTIES)])
    rows = result.stdout.splitlines()
    pkg.require(all('=' in line for line in rows), 'resource unit observation')
    value = dict(line.split('=', 1) for line in rows)
    pkg.require(len(rows) == len(value) and value.get('Id') == unit and
        (result.returncode == 0 or value.get('LoadState') == 'not-found'),
        'resource unit unavailable')
    return value


def scope_matches(value, unit, token):
    pkg.require(value['Id'] == unit and value['LoadState'] == 'loaded' and
        value['ActiveState'] == 'active' and value['Description'] == token and
        value['Delegate'] == 'yes' and value['OOMPolicy'] == 'continue' and
        {'memory', 'pids'} <= set(value['DelegateControllers'].split()) and
        re.fullmatch(r'[0-9a-f]{32}', value['InvocationID']), 'scope identity/delegation')
    return value


def inode(path):
    info = path.lstat()
    pkg.require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(), 'owned cgroup directory')
    return [info.st_dev, info.st_ino]


def cgroup_path(value, unit):
    prefix = f'/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/'
    pkg.require(isinstance(value, str) and value.startswith(prefix) and
        Path(value).name == unit and '..' not in value.split('/'), 'delegated cgroup ancestry')
    path = CGROUP / value.lstrip('/')
    pkg.require(path.resolve(strict=True) == path, 'cgroup path changed')
    inode(path)
    return path


def read(path, maximum=16384):
    with path.open('r', encoding='ascii') as stream:
        value = stream.read(maximum+1)
    pkg.require(len(value) <= maximum and value.endswith('\n'), 'bounded cgroup observation')
    return value.strip()


def number(raw):
    pkg.require(re.fullmatch(r'[0-9]+', raw) and len(raw) <= 20, 'cgroup integer')
    return int(raw)


def counters(raw):
    result = {}
    for line in raw.splitlines():
        key, value = line.split()
        pkg.require(key not in result and re.fullmatch(r'[a-z_]+', key), 'cgroup counter keys')
        result[key] = number(value)
    return result


def limits(value):
    return {'memory.high':value['memory_high_bytes'], 'memory.max':value['memory_max_bytes'],
            'memory.swap.max':0, 'memory.oom.group':1, 'pids.max':value['tasks_max']}


def write(path, value):
    # Only caller-validated owned delegated directories reach this helper.
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    try:
        raw = (str(value)+'\n').encode('ascii')
        pkg.require(os.write(fd, raw) == len(raw), 'short cgroup control write')
    finally: os.close(fd)


def membership(pid):
    lines = Path(f'/proc/{pid}/cgroup').read_text().splitlines()
    pkg.require(len(lines) == 1 and lines[0].startswith('0::/'), 'unified cgroup membership')
    return lines[0][3:]


def verify_record(record, *, verify_limits=True, require_manager=True):
    if require_manager:
        value = scope_matches(properties(record['unit']), record['unit'], record['token'])
        root = cgroup_path(value['ControlGroup'], record['unit'])
        pkg.require(value['InvocationID'] == record['invocation_id'],
                    'cgroup instance changed')
    else:
        # The scope was established with manager verification. Runtime safety
        # follows its kernel identity, membership and limits, not D-Bus health.
        from tools.serve_service import supervisor as sup
        root = cgroup_path('/'+str(Path(record['root']).relative_to(CGROUP)),
                           record['unit'])
        owner = record['owner']
        pkg.require(sup.process_identity(owner['pid']) == owner and
            membership(owner['pid']) == '/'+str((root/'control').relative_to(CGROUP)),
            'resource supervisor identity/membership changed')
    pkg.require(str(root) == record['root'] and inode(root) == record['root_identity'],
                'cgroup instance changed')
    for name in ('control', 'payload'):
        pkg.require(inode(root/name) == record[name+'_identity'], 'cgroup child replaced')
    if verify_limits:
        for name, expected in limits(record['policy']).items():
            pkg.require(number(read(root/'payload'/name)) == expected, 'backend resource limit drift')
    for name, expected in (('memory.max',record['policy']['control_max_bytes']),
                           ('memory.swap.max',0), ('pids.max',record['policy']['control_tasks_max'])):
        pkg.require(number(read(root/'control'/name)) == expected, 'control resource limit drift')
    return root


def sample(record, *, verify_limits=True, require_manager=True,
           tolerate_statistics=False):
    root = verify_record(record, verify_limits=verify_limits,
                         require_manager=require_manager)
    payload = root/'payload'
    value, statistics_errors = {}, {}
    for name in FIELDS:
        if name.endswith('events'):
            value[name] = counters(read(payload/name))
            continue
        try:
            value[name] = number(read(payload/name))
        except (OSError, ValueError, RuntimeError) as error:
            if not tolerate_statistics:
                raise
            value[name] = None
            statistics_errors[name] = type(error).__name__
    pkg.require({'high','max','oom','oom_kill'} <= value['memory.events'].keys() and
        'max' in value['pids.events'] and value['cgroup.events'].get('populated') in (0,1),
        'required resource counters missing')
    with (payload/'cgroup.procs').open() as stream:
        raw = stream.read(65537)
    pkg.require(len(raw) <= 65536, 'cgroup member bound')
    value['members'] = sorted({number(v) for v in raw.splitlines()})
    value['effective_limits'] = {name:number(read(payload/name)) for name in limits(record['policy'])}
    value['sampled_monotonic'] = time.monotonic()
    if tolerate_statistics:
        value['statistics_errors'] = statistics_errors
    return value


def violation(value):
    if value['memory.events']['oom'] or value['memory.events']['oom_kill']:
        return 'host_resource_oom'
    if value['pids.events']['max']:
        return 'host_resource_tasks'
    return None


class Envelope:
    def __init__(self, selected, output, *, isolate_manager=False):
        from tools.serve_service import supervisor as sup
        self.output = Path(output)
        self.selected = validate(selected)
        self.owner = sup.process_identity(os.getpid())
        self.error = None; self.latest = None; self.closed = False
        self.isolate_manager = isolate_manager
        self.statistics_gap_samples = 0
        token = secrets.token_hex(16); unit = 'q4t-host-'+token+'.scope'
        token = 'q4t-host-owner-'+token
        self.record = dict(unit=unit, token=token, owner=self.owner, policy=selected)
        pkg.save(self.output/'resource-intent.json', self.record)
        props = [('Description','s',token), ('PIDs','au','1',str(os.getpid())),
                 ('DelegateControllers','as','2','memory','pids'),
                 ('MemoryAccounting','b','true'), ('TasksAccounting','b','true'),
                 ('OOMPolicy','s','continue')]
        result = command(['/usr/bin/busctl', '--user', '--timeout=5', '--json=short', 'call',
            'org.freedesktop.systemd1', '/org/freedesktop/systemd1', 'org.freedesktop.systemd1.Manager',
            'StartTransientUnit', 'ssa(sv)a(sa(sv))', unit, 'fail', str(len(props)),
            *(part for prop in props for part in prop), '0'])
        pkg.require(result.returncode == 0, 'resource scope creation failed')
        # StartTransientUnit queues a job. Poll this same owned scope rather
        # than assuming the D-Bus reply implies the process has already moved.
        until = time.monotonic()+5
        while True:
            value = properties(unit)
            if value.get('ActiveState') == 'active':
                scope_matches(value, unit, token)
                if membership(os.getpid()) == value['ControlGroup']:
                    break
            pkg.require(value.get('LoadState') != 'loaded' or
                        value.get('Description') == token, 'resource scope replaced')
            pkg.require(time.monotonic() < until, 'resource scope activation deadline')
            time.sleep(.02)
        root = cgroup_path(value['ControlGroup'], unit)
        pkg.require(membership(os.getpid()) == value['ControlGroup'] and
            read(root/'cgroup.procs') == str(os.getpid()), 'scope contains another owner')
        control, payload = root/'control', root/'payload'
        control.mkdir(mode=0o700)
        write(control/'cgroup.procs', os.getpid())
        write(root/'cgroup.subtree_control', '+memory +pids')
        payload.mkdir(mode=0o700)
        for name, amount in limits(selected).items(): write(payload/name, amount)
        for name, amount in (('memory.max',selected['control_max_bytes']), ('memory.swap.max',0),
                             ('pids.max',selected['control_tasks_max'])): write(control/name, amount)
        self.record.update(root=str(root), root_identity=inode(root),
            control_identity=inode(control), payload_identity=inode(payload),
            invocation_id=value['InvocationID'])
        pkg.save(self.output/'resources.json', self.record)
        self.check(startup=True)

    def observe(self, *, startup=False):
        try:
            runtime = self.isolate_manager and not startup
            self.latest = sample(self.record, require_manager=not runtime,
                                 tolerate_statistics=runtime)
            if self.latest.get('statistics_errors'):
                self.statistics_gap_samples += 1
            self.error = self.error or violation(self.latest)
        except Exception:
            self.error = self.error or 'host_resource_observation_unavailable'
            raise
        return self.latest

    def check(self, *, startup=False):
        self.observe(startup=startup)
        pkg.require(self.error is None, self.error)

    def close(self):
        if self.closed: return
        # Limits can drift while ownership remains valid. Preserve the failure
        # and remove only our empty leaf, without repairing the changed policy.
        value = sample(self.record, verify_limits=False,
                       tolerate_statistics=self.isolate_manager)
        self.latest = value
        if value.get('statistics_errors'):
            self.statistics_gap_samples += 1
        self.error = self.error or violation(value)
        pkg.require(value['cgroup.events']['populated'] == 0 and not value['members'],
                    'backend resource group still populated')
        root = verify_record(self.record, verify_limits=False)
        (root/'payload').rmdir()
        self.closed = True
        pkg.save(self.output/'resources-final.json', dict(record=self.record,
            final=value, violation=self.error, payload_removed=True,
            scope_cleanup_pending=True,
            **({'statistics_gap_samples': self.statistics_gap_samples}
               if self.isolate_manager else {})))


def attach(output, selected):
    from tools.serve_service import supervisor as sup
    output = Path(output); record = pkg.read(output/'resources.json')
    pkg.require(record['policy'] == validate(selected) and
        record['owner'] == sup.process_identity(os.getppid()), 'resource supervisor binding')
    root = verify_record(record)
    pkg.require(membership(os.getpid()) == '/'+str((root/'control').relative_to(CGROUP)),
                'backend must inherit control group')
    pkg.require(int(Path('/proc/self/oom_score_adj').read_text()) > -1000,
                'backend must not be immune to cgroup OOM')
    write(root/'payload/cgroup.procs', os.getpid())
    pkg.require(membership(os.getpid()) == '/'+str((root/'payload').relative_to(CGROUP)),
                'backend resource attachment failed')
    verify_record(record)
    pkg.save(output/f'resource-backend-{os.getpid()}.json', dict(owner=sup.process_identity(os.getpid()),
        supervisor=record['owner'], unit=record['unit'], payload_identity=record['payload_identity'],
        membership=membership(os.getpid()), limits=limits(record['policy'])))


def departed(output):
    """Read-only controller proof after owned descendants have been reaped."""
    path = Path(output)/'resource-intent.json'
    if not path.exists(): return dict(enabled=False)
    intent = pkg.read(path)
    until = time.monotonic()+5
    while True:
        value = properties(intent['unit'])
        if value.get('LoadState') == 'not-found':
            return dict(enabled=True, scope_removed=True, unit=intent['unit'])
        pkg.require(value['Description'] == intent['token'], 'resource unit replaced')
        if value['ActiveState'] in ('inactive','failed') and not value.get('ControlGroup'):
            return dict(enabled=True, scope_removed=True, unit=intent['unit'], properties=value)
        pkg.require(time.monotonic() < until, 'resource scope cleanup unconfirmed')
        time.sleep(.05)
