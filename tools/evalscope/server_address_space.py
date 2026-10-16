"""Opt-in, server-only address-space diagnostics; never changes this process."""
import hashlib
import os
from pathlib import Path
import platform
import shutil

ADDR_NO_RANDOMIZE = 0x40000


def personality(pid):
    return int(Path(f'/proc/{pid}/personality').read_text().strip(), 16)


def launch_command(command, mode):
    if mode in (None, 'inherit'):
        return list(command)
    if mode != 'no-aslr':
        raise ValueError(f'unknown address-space mode: {mode}')
    launcher = shutil.which('setarch')
    if not launcher:
        raise RuntimeError('setarch missing; refusing an uncontrolled launch')
    return [launcher, platform.machine(), '-R', *command]


def start_ticks(pid):
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]


def snapshot(pid, binary, mode, parent_personality):
    before = start_ticks(pid)
    exe = Path(os.readlink(f'/proc/{pid}/exe'))
    if exe.resolve() != Path(binary).resolve():
        raise RuntimeError(f'snapshot executable mismatch: {exe}')
    actual = personality(pid)
    text = Path(f'/proc/{pid}/maps').read_text()
    if before != start_ticks(pid):
        raise RuntimeError('process identity changed during maps snapshot')
    expected_disabled = (mode == 'no-aslr' or
                         bool(parent_personality & ADDR_NO_RANDOMIZE))
    if bool(actual & ADDR_NO_RANDOMIZE) != expected_disabled:
        raise RuntimeError('server ASLR personality does not match launch mode')
    if personality(os.getpid()) != parent_personality:
        raise RuntimeError('runner personality unexpectedly changed')
    # File-backed executable mappings identify code placement without equating
    # them to CUDA allocations or physical pages. Preserve the entire raw map.
    executable = []
    for line in text.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) == 6 and 'x' in fields[1] and fields[5].startswith('/'):
            executable.append({'range': fields[0], 'permissions': fields[1],
                               'offset': fields[2], 'device': fields[3],
                               'inode': fields[4], 'path': fields[5]})
    if not executable:
        raise RuntimeError('no file-backed executable mappings observed')
    return {'pid': pid, 'start_ticks': before, 'exe': str(exe), 'mode': mode,
            'personality': actual, 'parent_personality': parent_personality,
            'aslr_disabled': bool(actual & ADDR_NO_RANDOMIZE),
            'maps_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'executable_mappings': executable,
            'scope': 'ready-time virtual maps only; not physical or CUDA placement'}, text


def require_mapping_match(current, reference):
    """Fail before HTTP if a repeated controlled identity did not stabilize."""
    if (current['mode'] != 'no-aslr' or reference['mode'] != 'no-aslr' or
            not current['aslr_disabled'] or not reference['aslr_disabled'] or
            current['exe'] != reference['exe'] or
            not current['executable_mappings'] or
            current['executable_mappings'] != reference['executable_mappings']):
        raise RuntimeError('controlled server executable mappings differ from reference')
