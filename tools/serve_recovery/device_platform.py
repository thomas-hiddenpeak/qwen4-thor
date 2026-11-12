"""Independent OpenRM handle-ownership facts for explicit device deployment.

This reviewed profile binds the installed Thor driver, not merely a version
label. Zero references establish absence of user-held driver capabilities under
the separate complete fence. They do NOT establish idle hardware or completion
of deferred driver cleanup: the Guardian still requires its bounded CUDA probe.
No NVML, CUDA, device open, module reload or process signal occurs here.
"""
import hashlib
import os
from pathlib import Path
import stat
import sys

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.serve_recovery import device_inventory as inventory, ledger

PROFILE = 'thor-openrm-595.78-linux-6.8.12-1021-v1'
KERNEL = '6.8.12-1021-tegra'
# Qualified against the actual installed artifacts and NVIDIA R39.2.1 source.
# Updating a driver requires a reviewed new profile, not a version wildcard.
MODULES = {
    'nvidia': ('nvidia.ko', '01406A18AAA4C43F4A8F257',
        '9ef9396ca6475c58152e8649d8fa437df1912141af2830955b21ebc81e466f88'),
    'nvidia_uvm': ('nvidia-uvm.ko', 'A623A6CBE45E7F9D094A0EC',
        'eb20fa7ac5dc206e2de0c6c8669467d7c88025c3ed0ed9642779da9a409d2126'),
    'nvidia_modeset': ('nvidia-modeset.ko', '5AD5F0AFD2BA67346812F74',
        'a7a997e606ca01290e3adf8c97addf4d9c72fa20ca8cccf4e63cb799986ab28e'),
    'nvidia_drm': ('nvidia-drm.ko', '5A2078824FD7C27DF657AFC',
        '4ed8894621b6c20f04aef16f3740bc1e69c499dfa8f06464f92a74375adf23d6'),
}


def artifact(path):
    path = Path(path).resolve(strict=True)
    for parent in (path, *path.parents):
        info = parent.lstat()
        ledger.require(info.st_uid == 0 and not info.st_mode & 0o022,
                       'driver artifact ancestry is not administrator-owned')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        ledger.require(stat.S_ISREG(before.st_mode) and
                       0 < before.st_size <= 128*1024**2, 'driver artifact bound')
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            raw = os.read(fd, min(remaining, 1024**2))
            ledger.require(raw, 'driver artifact shortened')
            digest.update(raw); remaining -= len(raw)
        after = os.fstat(fd)
        ledger.require(not os.read(fd, 1) and all(getattr(before, key) == getattr(after, key)
            for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')),
            'driver artifact changed')
        return digest.hexdigest()
    finally:
        os.close(fd)


def validate(facts, hashes, *, alternate_driver=False):
    ledger.require(facts['kernel_release'] == KERNEL and not alternate_driver,
                   'device ownership profile needs platform qualification')
    active = {item['name']: item for item in facts['modules']}
    ledger.require(set(active) <= MODULES.keys() and
                   inventory.REQUIRED <= active.keys() and set(hashes) == set(active),
                   'driver profile inventory differs')
    for name, item in active.items():
        _filename, source, digest = MODULES[name]
        ledger.require(item['version'] == '595.78' and item['source'] == source and
                       hashes[name] == digest, 'driver artifact is not qualified')
    return dict(profile=PROFILE, artifacts=hashes)


def inspect():
    before = inventory.snapshot()
    directory = Path('/lib/modules')/before['kernel_release']/'updates/opensource-gpu-disp'
    hashes = {item['name']: artifact(directory/MODULES[item['name']][0])
              for item in before['modules']}
    profile = validate(before, hashes, alternate_driver=Path('/sys/module/nvgpu').exists())
    after = inventory.snapshot()
    ledger.require(before == after, 'device facts changed during platform qualification')
    outstanding = [dict(module=item['name'], references=item['non_dependency_references'])
        for item in before['modules'] if item['non_dependency_references']]
    return dict(version=1, profile=profile, facts=before,
        status='BUSY' if outstanding else 'NO_USER_HANDLES_OBSERVED',
        outstanding=outstanding, launch_authorized=False, model_actions=False)


def main():
    import json
    ledger.require(len(sys.argv) == 1, 'platform worker accepts no system path overrides')
    try:
        result = inspect()
    except (OSError, ValueError, KeyError, UnicodeError) as error:
        result = dict(version=1, status='UNKNOWN', reason=str(error)[:1024],
                      launch_authorized=False, model_actions=False)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
