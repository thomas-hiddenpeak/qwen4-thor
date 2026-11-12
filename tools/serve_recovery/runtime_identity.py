"""Permanent runtime UID drop retaining only existing graphics access groups.

Jetson's nvmap requires the ordinary user's video membership. Emptying every
supplementary group prevents CUDA startup. Retain only existing video/render
membership, never the administrator's inherited groups or a new entitlement.
This lookup runs only at explicit privileged entry, outside healthy serving.
"""
import ctypes
import grp
import os
import pwd

from tools.serve_recovery import ledger

DEVICE_GROUPS = ('video', 'render')


def groups(uid, gid):
    ledger.require(ledger.integer(uid, minimum=1) and ledger.integer(gid, minimum=1) and
        uid < 2**32-1 and gid < 2**32-1, 'non-root runtime identity required')
    account = pwd.getpwuid(uid)
    ledger.require(account.pw_uid == uid and type(account.pw_name) is str and
        0 < len(account.pw_name) <= 256, 'runtime account differs')
    assigned = os.getgrouplist(account.pw_name, account.pw_gid)
    ledger.require(len(assigned) <= 256 and all(ledger.integer(n) and n < 2**32-1 for n in assigned),
                   'runtime group lookup exceeds bound')
    selected = set()
    for name in DEVICE_GROUPS:
        try: group = grp.getgrnam(name)
        except KeyError: continue
        ledger.require(ledger.integer(group.gr_gid, minimum=1) and group.gr_gid < 2**32-1,
                       'device group aliases root or has invalid identity')
        if group.gr_gid in assigned and group.gr_gid != gid: selected.add(group.gr_gid)
    return sorted(selected)


def drop(uid, gid):
    ledger.require(os.geteuid() == 0, 'explicit privileged runtime entry required')
    selected = groups(uid, gid)
    libc = ctypes.CDLL(None, use_errno=True)
    ledger.require(libc.prctl(39, 0, 0, 0, 0) == 1, 'runtime entry requires no_new_privs')
    os.setgroups(selected)
    os.setresgid(gid, gid, gid); os.setresuid(uid, uid, uid)
    ledger.require(os.getresuid() == (uid,)*3 and os.getresgid() == (gid,)*3 and
        sorted(os.getgroups()) == selected, 'permanent runtime identity drop failed')
    return selected
