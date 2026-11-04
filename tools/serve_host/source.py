"""Read a single pinned managed generation; never change or signal its state."""
import hashlib
import os
from pathlib import Path
import time

from tools.serve_host import core as c
from tools.serve_service import resources as r,supervisor as sup,inspection
from tools.serve_watchdog.storage import Directory
from tools.serve_watchdog.core import parse


def clock():
    return dict(boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                monotonic=time.monotonic(),boottime=time.clock_gettime(time.CLOCK_BOOTTIME),unix=time.time())


def optional(directory,name):
    try:return parse(directory.read(name))
    except FileNotFoundError:return None


def live(owner):
    inspection.owner(owner)
    try:return sup.process_identity(owner['pid'])==owner and inspection.process_row(owner['pid'])['state']!='Z'
    except (FileNotFoundError,ProcessLookupError):return False


def record(directory,policy,bound):
    raw=directory.read('controller.json');control=parse(raw)
    c.require(control.get('version')==2 and control['package']==policy['backend_package'] and
        control['manifest_sha256']==policy['backend_manifest_sha256'] and
        control['observation_context']==inspection.current_context(),'binding_invalid')
    inspection.owner(control['owner'])
    info=os.fstat(directory.fd)
    binding=dict(run_identity=[info.st_dev,info.st_ino],controller_sha256=hashlib.sha256(raw).hexdigest(),
                 controller=control['owner'],resource_sha256=None)
    if bound is not None:
        c.require(all(bound[key]==binding[key] for key in ('run_identity','controller_sha256','controller')),'binding_invalid')
        binding=bound.copy()
    return control,binding


def evaluate(directory,policy,bound,now):
    control,binding=record(directory,policy,bound)
    c.require(control['owner']['boot_id']==now['boot_id'],'boot_changed')
    worker=optional(directory,'status.json')
    terminal=optional(directory,'terminal.json')
    resource=optional(directory,'resources.json')
    if worker is not None:
        c.require(worker['package_manifest_sha256']==policy['backend_manifest_sha256'],'binding_invalid')
        inspection.owner(worker['owner'])
    if resource is not None:
        c.require(resource['policy']==policy['host_resources'] and worker is not None and
            resource['owner']==worker['owner'],'binding_invalid')
        checksum=hashlib.sha256(directory.read('resources.json')).hexdigest()
        c.require(binding['resource_sha256'] in (None,checksum),'binding_invalid')
        binding['resource_sha256']=checksum
    if terminal is not None:
        c.require(terminal['state'] in ('STOPPED','FAILED') and type(terminal['cleanup_confirmed']) is bool and
            worker is not None and terminal['worker']==worker,'binding_invalid')
        # An old terminal must not disguise a live/replaced process.
        c.require(not live(control['owner']) and not live(worker['owner']) and
            (worker.get('backend') is None or not live(worker['backend'])),'owner_absent')
        final=optional(directory,'resources-final.json')
        values=None;reason='none' if terminal['state']=='STOPPED' else 'backend_failure'
        if final is not None:
            c.require(resource is not None and final['record']==resource and final['payload_removed'] is True and
                not final['final']['members'] and final['final']['cgroup.events']['populated']==0,'binding_invalid')
            values=c.snapshot(final['final'],policy['host_resources'])
            failure=final['violation']
            if failure in ('host_resource_oom','host_resource_tasks','host_resource_observation_unavailable'):
                reason=failure
            c.require(failure is None or terminal['state']=='FAILED','binding_invalid')
        return c.view(terminal['state'],reason,kind='final' if values is not None else 'none',
            values=values,cleanup=terminal['cleanup_confirmed']),binding
    c.require(live(control['owner']),'owner_absent')
    if worker is None:return c.view('STARTING'),binding
    c.require(live(worker['owner']) and inspection.process_row(worker['owner']['pid'])['ppid']==control['owner']['pid'],
              'owner_absent')
    state=worker['state']
    if resource is None:
        c.require(state in ('STARTING','STOPPING','FAILED'),'binding_invalid')
        # Even early state has a wall timestamp; it cannot remain fresh forever.
        age=now['unix']-c.number(worker['updated_unix'])
        c.require(0<=age<=c.STALE_SECONDS,'source_stale')
        return c.view('STARTING' if state=='STARTING' else 'STOPPING',source_age=age),binding
    h=worker['host_resources'];observation=h['observation']
    c.require(h['policy']==policy['host_resources'] and observation is not None,'binding_invalid')
    age=now['monotonic']-c.number(observation['sampled_monotonic'])
    c.require(0<=age<=c.STALE_SECONDS,'source_stale')
    # v5 records monotonic time, which excludes suspend. The worker wall
    # timestamp is an additional guard: suspend or clock jumps become unknown
    # until the live worker publishes again, rather than extending old READY.
    wall_age=now['unix']-c.number(worker['updated_unix'])
    c.require(-30<=wall_age<=c.STALE_SECONDS,'source_stale')
    age=max(age,wall_age)
    c.require(r.membership(worker['owner']['pid'])=='/'+str((Path(resource['root'])/'control').relative_to(r.CGROUP)),
              'binding_invalid')
    backend=worker.get('backend')
    if backend is not None:
        c.require(live(backend) and inspection.process_row(backend['pid'])['ppid']==worker['owner']['pid'] and
            r.membership(backend['pid'])=='/'+str((Path(resource['root'])/'payload').relative_to(r.CGROUP)),'owner_absent')
    raw=r.sample(resource)
    values=c.snapshot(raw,policy['host_resources'])
    disposition={'STARTING':'STARTING','READY':'RUNNING','DEGRADED':'RUNNING',
        'OBSERVATION_UNAVAILABLE':'RUNNING','STOPPING':'STOPPING','RESTART_WAIT':'STARTING','FAILED':'FAILED'}.get(state)
    c.require(disposition is not None,'binding_invalid')
    failure=r.violation(raw) or h['violation']
    reason='none'
    if failure is not None:
        c.require(failure in ('host_resource_oom','host_resource_tasks','host_resource_observation_unavailable'),
                  'resource_observation_invalid')
        disposition,reason='FAILED',failure
    return c.view(disposition,reason,kind='current',values=values,ready=state=='READY',source_age=age),binding


def observe(policy,bound,now=None):
    now=clock() if now is None else now
    try:
        path=Path(policy['backend_run'])
        if not os.path.lexists(path):
            c.require(bound is None,'source_unavailable')
            value=c.view('WAITING');binding=None
        else:
            with Directory(path) as directory:
                try:_,binding=record(directory,policy,bound)
                except FileNotFoundError:
                    if bound is not None:raise
                    value,binding=c.view('STARTING'),None
                else:
                    # Pin the first validated controller even if subsequent
                    # cgroup observation fails. Missing files cannot rebind it.
                    bound=binding
                    value,binding=evaluate(directory,policy,binding,now)
                directory.consistent()
    except c.Invalid as error:
        value,binding=c.view('UNKNOWN',str(error)),bound
    except (OSError,ValueError,KeyError,TypeError,RuntimeError,IndexError):
        value,binding=c.view('UNKNOWN','source_unavailable'),bound
    value.update(evaluated_unix=now['unix'],evaluated_boottime=now['boottime'],boot_id=now['boot_id'])
    return value,binding
