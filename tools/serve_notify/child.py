"""Exec one pinned monitor with parent-death and inherited state ownership."""
import argparse
import fcntl
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_notify import notify as n


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    value = n.m.read(args.run/'controller.json')
    n.sup.arm_parent_death(value['owner'])
    package, digest = Path(value['package']), value['manifest_sha256']
    manifest = n.verify(package, digest)
    state = n.check_state(value['persistent_state'], package, digest)
    fd = int(os.environ.pop('Q4T_NOTIFY_CLAIM_FD'))
    info = os.fstat(fd)
    path = state/(value['role']+'.lock')
    n.require([info.st_dev, info.st_ino] == value['claim_identity'] ==
              [path.stat().st_dev, path.stat().st_ino], 'state claim replaced')
    with path.open('r+') as probe:
        try: fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: pass
        else: raise RuntimeError('inherited state claim missing')
    os.set_inheritable(fd, True)
    argv = n.command(package, manifest, value['role'], state)
    os.execv(argv[0], argv)


if __name__ == '__main__': main()
