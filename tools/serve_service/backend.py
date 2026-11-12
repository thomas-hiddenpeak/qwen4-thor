"""Exec the pinned backend with a parent-death signal across exec."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tools/evalscope'))
from tools.serve_service import package as pkg, supervisor
import gpu_exclusive as guard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    control = pkg.read(args.run/'controller.json')
    worker = pkg.read(args.run/'status.json')['owner']
    supervisor.arm_parent_death(worker)
    fields = Path(f"/proc/{worker['pid']}/stat").read_text().rsplit(')', 1)[1].split()
    pkg.require(int(fields[1]) == control['owner']['pid'] and
                supervisor.process_identity(control['owner']['pid']) == control['owner'],
                'backend controller ancestry changed')
    manifest = pkg.verify(control['package'], control['manifest_sha256'])
    guard.ROOT = Path(manifest['repository'])
    lease_fd = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    pkg.require(control['lock_path'] == str(guard.common_lock()),
                'backend repository lease differs')
    with guard.borrowed_lock(lease_fd, Path(control['lock_path'])):
        pass
    os.set_inheritable(lease_fd, True)
    del os.environ['Q4T_MANAGED_LEASE_FD']
    if 'host_resources' in manifest:
        from tools.serve_service import resources
        resources.attach(args.run, manifest['host_resources'])
    command = pkg.command(control['package'], manifest, state_directory=args.run)
    os.execv(command[0], command)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'managed backend rejected: {error}', file=sys.stderr)
        raise SystemExit(1)
