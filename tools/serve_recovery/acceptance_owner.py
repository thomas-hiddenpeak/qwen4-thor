"""Read-only acceptance provenance under the private files' actual owner.

Validation-only helper. It grants no model, lease or device-domain authority.
Privileged host drivers call this before any such side effect.
"""
import os
from pathlib import Path
import traceback


def verify(plan, checker, reap, finish_child):
    uid,gid = plan['runtime_uid'],plan['runtime_gid'];out = Path(plan['output'])
    if not (os.geteuid() == 0 and type(uid) is int and type(gid) is int and gid > 0 and
            uid == int(os.environ['SUDO_UID']) > 0 and out.stat().st_uid == uid and
            out.stat().st_gid == gid):raise ValueError('explicit original administrator required')
    pid = os.fork()
    if pid == 0:
        try:
            os.setgroups([]);os.setresgid(gid,gid,gid);os.setresuid(uid,uid,uid)
            checker(plan)
        except BaseException:
            traceback.print_exc();os._exit(1)
        os._exit(0)
    try:
        code = reap(pid,30)
        if code is None:raise TimeoutError('bounded source verification deadline')
        pid = None
        if code != 0:raise ValueError('original user package verification failed')
    finally:
        if pid is not None:finish_child(pid)
