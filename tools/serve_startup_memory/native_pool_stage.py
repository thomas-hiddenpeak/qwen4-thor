"""Run two isolated pool observations under a held GPU lease with zero models."""
import argparse
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.serve_recovery import resource_kernel as kernel
from tools.serve_startup_memory import pool_accounting as pool


def owned_domain(binary, output):
    if os.geteuid() != 0:
        raise ValueError('temporary kernel resource setup requires root')
    uid, gid = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
    if uid <= 0 or gid <= 0:
        raise ValueError('expected an unprivileged invoking user')
    user = pwd.getpwuid(uid)
    record = kernel.create(str(uuid.uuid4()), str(uuid.uuid4()), uid,
                           kernel.policy(1536, 2048, 64))
    root = Path(record['root'])
    try:
        with kernel.Scope(record) as scope:
            pid = os.fork()
            if not pid:
                try:
                    entry = scope.join_descriptor('outer')
                    try:
                        scope.join_self('outer',entry)
                    finally:
                        os.close(entry)
                    os.initgroups(user.pw_name,gid)
                    os.setresgid(gid,gid,gid)
                    os.setresuid(uid,uid,uid)
                    rows = []
                    for mode in ('async','async-retained'):
                        rows.append(pool.observe(binary,output/mode,mode,scope))
                    print(json.dumps(dict(record=record,observations=rows)),flush=True)
                    os._exit(0)
                except BaseException:
                    traceback.print_exc()
                    os._exit(1)
            _, status = os.waitpid(pid,0)
            if os.waitstatus_to_exitcode(status) != 0:
                raise RuntimeError('unprivileged observation failed')
    finally:
        # Revalidate the exact original tree before attempting empty rmdir.
        with kernel.Scope(record) as scope:
            if not all(scope.empty(role) for role in kernel.NAMES):
                raise RuntimeError('owned resource domain remains populated')
            for role in kernel.NAMES:
                (root/role).rmdir()
        root.rmdir()


def run(binary, output):
    from tools.evalscope import gpu_exclusive
    from tools.serve_recovery.child import verify_lease
    from tools.serve_service import memory, package
    uid = os.getuid()
    if not uid or os.geteuid() != uid:
        raise ValueError('start this driver as the ordinary GPU lease holder')
    verify_lease(int(os.environ['Q4T_MANAGED_LEASE_FD']),gpu_exclusive.common_lock())
    if memory.read()['available_bytes'] < 16*1024**3:
        raise RuntimeError('insufficient host headroom for isolated characterization')
    binary = binary.resolve(strict=True)
    output = output.absolute()
    output.mkdir(mode=0o700)
    hashes = {str(p):package.sha(p) for p in
              (binary,Path(__file__),Path(pool.__file__),Path(kernel.__file__),
               Path(__file__).with_name('memory_probe.cu'))}
    with (output/'root.stderr').open('xb') as errors:
        result = subprocess.run(['sudo','-n','/usr/bin/python3','-I','-B',__file__,
                                 '--domain','--binary',binary,'--output',output],
                                stdout=subprocess.PIPE,stderr=errors,check=True)
    record = json.loads(result.stdout)
    rows = record['observations']
    if [row['mode'] for row in rows] != ['async', 'async-retained']:
        raise ValueError('unexpected pool modes or order')
    if rows[0]['device'] != rows[1]['device']:
        raise ValueError('device identity changed between probes')
    for row in rows:
        if pool.summarize(row['mode'], row['phases']) != row['deltas_from_baseline']:
            raise ValueError('recorded pool deltas differ from measurements')
    if Path(record['record']['root']).exists() or any(
            package.sha(p) != value for p,value in hashes.items()):
        raise ValueError('resource cleanup or input identity changed')
    record.update(status='PASS',sources=hashes,model_starts=0,owned_domains_removed=1,
                  physical_ram_limit_proved=False)
    package.save(output/'result.json',record)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--domain', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    (owned_domain if args.domain else run)(args.binary,args.output)
