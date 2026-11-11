"""Bounded, read-only device/module facts; never a cold-start authorization.

The deployment controller must first enforce a complete device fence, keep its
allowed cgroup empty, and independently validate this worker's identity. A
quiet module count without that fence is just a momentary observation. This
module never opens a GPU device, loads NVML/CUDA, or signals another process.
Run it as a disposable bounded job: sysfs/procfs can also become unavailable.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from tools.serve_recovery import ledger

MODULES = frozenset(('nvidia', 'nvidia_uvm', 'nvidia_modeset', 'nvidia_drm'))
REQUIRED = frozenset(('nvidia', 'nvidia_uvm'))
DEVICES = frozenset(('nvidia', 'nvidiactl', 'nvidia-modeset', 'nvidia-uvm',
                    'nvidia-nvswitch', 'nvidia-nvlink', 'nvidia-caps',
                    'nvidia-caps-imex-channels', 'drm'))


def read(path, maximum=4096):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        # os.read is intentionally bounded; a containing job supplies the
        # wall-clock deadline rather than blocking the Guardian on filesystem IO.
        data = os.read(fd, maximum+1)
        ledger.require(len(data) <= maximum, 'device metadata exceeds bound')
        ledger.require(not os.read(fd, 1), 'device metadata changed while reading')
        return data.decode('utf-8', errors='strict')
    finally:
        os.close(fd)


def unsigned(text):
    ledger.require(re.fullmatch(r'(0|[1-9][0-9]{0,9})\n?', text) is not None,
                   'invalid kernel reference count')
    value = int(text)
    ledger.require(value <= 2**31-1, 'kernel reference count exceeds bound')
    return value


def character_devices(text):
    ledger.require(len(text) <= 65536, 'device registry exceeds bound')
    lines = text.splitlines()
    ledger.require(lines and lines[0] == 'Character devices:' and
                   lines.count('Block devices:') == 1,
                   'invalid kernel device registry')
    index = lines.index('Block devices:')
    ledger.require(index <= 512, 'character registry exceeds bound')
    selected = {}
    named = {}
    for line in lines[1:index]:
        if not line:
            continue
        match = re.fullmatch(r'\s*([0-9]{1,4}) (\S{1,128})', line)
        ledger.require(match is not None, 'invalid character registry row')
        number, name = int(match[1]), match[2]
        ledger.require(1 <= number < 4096, 'invalid character device major')
        if name not in DEVICES:
            # Unknown NVIDIA entry points need a reviewed fence extension.
            ledger.require(not name.startswith(('nvidia', 'gdrdrv', 'nvgpu',
                                                 'nvhost-gpu')),
                           'unreviewed GPU character device')
            continue
        ledger.require(name not in named, 'repeated GPU device registry name')
        named[name] = number
        selected.setdefault(number, []).append(name)
    ledger.require({'nvidia', 'nvidiactl', 'nvidia-uvm'} <= named.keys(),
                   'required NVIDIA device registry missing')
    ledger.require(named['nvidia'] == named['nvidiactl'],
                   'NVIDIA control and device majors disagree')
    return [dict(major=number, names=sorted(names))
            for number, names in sorted(selected.items())]


def modules(directory):
    directory = Path(directory)
    active = {name for name in MODULES if (directory/name).exists()}
    ledger.require(REQUIRED <= active, 'required GPU driver module missing')
    result = []
    for name in sorted(active):
        path = directory/name
        before = path.stat(follow_symlinks=False)
        ledger.require(stat.S_ISDIR(before.st_mode), 'invalid driver module path')
        ledger.require(read(path/'initstate') == 'live\n',
                       'driver module not live')
        holders = sorted(entry.name for entry in (path/'holders').iterdir())
        ledger.require(len(holders) <= len(MODULES) and
                       set(holders) <= active and name not in holders,
                       'unreviewed or missing GPU module holder')
        for holder in holders:
            link = path/'holders'/holder
            ledger.require(link.is_symlink() and
                link.resolve(strict=True) == (directory/holder).resolve(strict=True),
                'module holder does not name the actual driver module')
        count = unsigned(read(path/'refcnt'))
        ledger.require(count >= len(holders), 'driver dependency accounting changed')
        version = read(path/'version').strip()
        source = read(path/'srcversion').strip()
        ledger.require(re.fullmatch(r'[0-9]{2,4}\.[0-9]{1,4}(\.[0-9]{1,4})?',
                                   version) is not None and
                       re.fullmatch(r'[A-Fa-f0-9]{1,64}', source) is not None,
                       'invalid driver build identity')
        after = path.stat(follow_symlinks=False)
        ledger.require((before.st_dev, before.st_ino) ==
                       (after.st_dev, after.st_ino), 'driver module replaced')
        result.append(dict(name=name, version=version, source=source,
            filesystem_device=before.st_dev, inode=before.st_ino,
            references=count, holders=holders,
            non_dependency_references=count-len(holders)))
    # A malicious/corrupt graph must not manufacture reference exemptions.
    graph = {item['name']: item['holders'] for item in result}
    def visit(name, ancestors):
        ledger.require(name not in ancestors, 'cyclic driver module graph')
        for holder in graph[name]:
            visit(holder, ancestors | {name})
    for name in graph:
        visit(name, set())
    ledger.require(len({item['version'] for item in result}) == 1,
                   'GPU driver module versions disagree')
    return result


def snapshot(*, sys_root=Path('/sys'), proc_root=Path('/proc')):
    sys_root, proc_root = Path(sys_root), Path(proc_root)
    boot = read(proc_root/'sys/kernel/random/boot_id').strip()
    ledger.require(ledger.identifier(boot), 'invalid device boot identity')
    release = read(proc_root/'sys/kernel/osrelease').strip()
    ledger.require(re.fullmatch(r'[a-zA-Z0-9._+-]{1,128}', release) is not None,
                   'invalid kernel release')
    return dict(version=1, boot_id=boot, kernel_release=release,
        modules=modules(sys_root/'module'),
        character_devices=character_devices(read(proc_root/'devices', 65536)))


def inspect(*, sys_root=Path('/sys'), proc_root=Path('/proc')):
    try:
        before = snapshot(sys_root=sys_root, proc_root=proc_root)
        after = snapshot(sys_root=sys_root, proc_root=proc_root)
        ledger.require(before == after, 'device observations changed')
        busy = [dict(module=item['name'],
                     references=item['non_dependency_references'])
                for item in before['modules']
                if item['non_dependency_references']]
        return dict(version=1, status='BUSY' if busy else 'QUIET_OBSERVATION',
            facts=before, facts_sha256=hashlib.sha256(
                ledger.canonical(before)).hexdigest(), outstanding=busy,
            launch_authorized=False, model_actions=False,
            reason='kernel_driver_references' if busy else
                   'requires_complete_kernel_fence_and_initial_qualification')
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        return dict(version=1, status='UNKNOWN', facts=None,
            facts_sha256=None, outstanding=None, launch_authorized=False,
            model_actions=False, reason=str(error)[:1024])


def main():
    if len(sys.argv) != 1:
        raise SystemExit('device inventory accepts no alternate system paths')
    print(json.dumps(inspect(), ensure_ascii=True, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
