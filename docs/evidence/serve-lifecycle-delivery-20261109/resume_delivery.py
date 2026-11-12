"""Preserve first refusal; remove group write only from owned default root."""
import os
import stat
from pathlib import Path
import deliver as delivery

os.umask(0o077)
root = delivery.DEFAULT
info = root.lstat()
delivery.p.require(root.resolve() == root and stat.S_ISDIR(info.st_mode) and
                   info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o775,
                   'unexpected default directory identity or mode')
delivery.p.require(not (root/'.q4t-publication').exists() and
    not (delivery.STATE/'service-publication-intents').exists(),
    'first publication unexpectedly progressed')
delivery.p.require(delivery.p.read(delivery.OUT/'result.json')['status'] == 'FAILED',
                   'first failure not preserved')
repair = delivery.BASE/'publication-root-repair.json'
delivery.p.require(not repair.exists(), 'repair already recorded')
delivery.p.save(repair, dict(status='PLANNED_OWNED_ROOT_PERMISSION_REPAIR',
    path=str(root), uid=info.st_uid, inode=info.st_ino, previous_mode='0775',
    selected_mode='0755', recursive=False,
    failure='owned canonical publication directory required',
    first_failure=str(delivery.OUT/'result.json'), model_starts=0, chat_requests=0))
fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
try:
    delivery.p.require((os.fstat(fd).st_dev, os.fstat(fd).st_ino) ==
                       (info.st_dev, info.st_ino), 'root changed before permission update')
    os.fchmod(fd, 0o755)
    os.fsync(fd)
finally:
    os.close(fd)
delivery.OUT = delivery.BASE/'delivery-02'
delivery.main()
