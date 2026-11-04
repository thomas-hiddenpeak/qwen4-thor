"""Validation-only bridge: run the sealed controller on an owned lease FD.

The controller, worker, supervisor and backend code remain the sealed product.
Only its run_guard call receives the existing supported lease_fd parameter;
this is not an installed service entry or a new public deployment option.
"""
import argparse
import os
from pathlib import Path
import sys

sys.dont_write_bytecode=True


def borrowed_run(service,args):
    guard=service.guard
    with guard.borrowed_lock(args.lease_fd,guard.common_lock()):
        pass
    original=guard.run_guard
    def run_guard(command,output,lock_path,**kwargs):
        if 'lease_fd' in kwargs:
            raise RuntimeError('unexpected nested lease override')
        return original(command,output,lock_path,lease_fd=args.lease_fd,**kwargs)
    guard.run_guard=run_guard
    try:
        return service.run(args.package,args.digest,args.output)
    finally:
        guard.run_guard=original


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package',type=Path,required=True)
    p.add_argument('--digest',required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--parent',type=int,required=True)
    p.add_argument('--lease-fd',type=int,required=True)
    args=p.parse_args()
    sys.path.insert(0,str(args.package/'source'))
    from tools.serve_service import service,package as pkg,supervisor as sup
    os.umask(0o077)
    sup.arm_parent_death(sup.process_identity(args.parent))
    manifest=pkg.verify(args.package,args.digest)
    service.guard.ROOT=Path(manifest['repository'])
    return borrowed_run(service,args)


if __name__=='__main__':
    raise SystemExit(main())
