"""Read-only public package material and one sticky managed gateway generation."""
import hashlib
import os
from pathlib import Path

from tools.serve_tls import core as c
from tools.serve_host import source as host
from tools.serve_gateway import gateway as g,managed as m,tls
from tools.serve_service import package as pkg
from tools.serve_watchdog.storage import Directory,sha
from tools.serve_watchdog.core import parse

clock=host.clock
optional=host.optional
live=host.live
inspection=host.inspection


def package(path,digest):
    """Validate known producer and public bytes, including already-expired material.

    No reads of nginx.conf, client-policy.json, or the private key. A pinned
    producer identity is required; this does not issue a new client-trust claim.
    """
    path=Path(path).absolute()
    with Directory(path) as outer:
        raw=outer.read('manifest.json');c.require(sha(raw)==digest,'binding_invalid');manifest=parse(raw)
        c.require(manifest['version']==4 and set(manifest['sources'])==set(m.V4_SOURCES) and
            manifest['gateway']['package']==str(path/'gateway'),'binding_invalid')
        for name in ('tools/serve_gateway/gateway.py','tools/serve_gateway/managed.py','tools/serve_gateway/tls.py'):
            c.require(manifest['sources'][name]==pkg.sha(pkg.ROOT/name),'binding_invalid')
        for name,checksum in manifest['sources'].items():
            c.require(hashlib.sha256(g.read_file(path/'source'/name,private=True,maximum=2*1024**2)).hexdigest()==checksum,'binding_invalid')
        with Directory(path/'gateway') as inner:
            raw=inner.read('manifest.json');c.require(sha(raw)==manifest['gateway']['manifest_sha256'],'binding_invalid')
            value=parse(raw);material=c.material(value['tls'])
            c.require(value['version']==3 and value['generator_sha256']==manifest['sources']['tools/serve_gateway/gateway.py'] and
                value['tls_tool_sha256']==manifest['sources']['tools/serve_gateway/tls.py'] and value['nginx']['path']==str(path/'nginx'),
                'binding_invalid')
            files={name:value['files'][name] for name in ('certificate.pem',*(('trusted-ca.pem',) if material['anchor'] else ()))}
            values={name:inner.read(name,maximum=tls.MAX_BYTES) for name in files}
            c.require(all(sha(values[n])==v for n,v in files.items()),'binding_invalid')
            chain=[tls.dates(*v) for v in tls.certificates(values['certificate.pem'])]
            anchor=None if material['anchor'] is None else tls.dates(*tls.certificates(values['trusted-ca.pem'],1)[0])
            c.require(chain==material['chain'] and anchor==material['anchor'],'binding_invalid')
            return dict(gateway_package=str(path),gateway_manifest_sha256=digest,
                inner_manifest_sha256=manifest['gateway']['manifest_sha256'],public_files=files,material=material,
                nginx_sha256=value['nginx']['sha256'],listen_host=value['policy']['listen_host'],listen_port=value['policy']['listen_port'],
                backend_port=value['policy']['upstream_port'],repository=manifest['repository'])


def material_current(policy):
    path=Path(policy['gateway_package'])
    with Directory(path) as outer:
        c.require(sha(outer.read('manifest.json'))==policy['gateway_manifest_sha256'],'binding_invalid')
    with Directory(path/'gateway') as inner:
        c.require(sha(inner.read('manifest.json'))==policy['inner_manifest_sha256'],'binding_invalid')
        for name,digest in policy['public_files'].items():c.require(sha(inner.read(name,maximum=tls.MAX_BYTES))==digest,'binding_invalid')
    return c.material(policy['material'])


def record(directory,policy,bound,now):
    raw=directory.read('controller.json');control=parse(raw);info=os.fstat(directory.fd)
    c.require(control['version']==1 and control['package']==policy['gateway_package'] and
        control['manifest_sha256']==policy['gateway_manifest_sha256'],'binding_invalid')
    inspection.owner(control['owner']);c.require(control['owner']['boot_id']==now['boot_id'],'boot_changed')
    binding=dict(run_identity=[info.st_dev,info.st_ino],controller_sha256=sha(raw),controller=control['owner'],
        nginx=None,listener=None,last_status_sha256=None,last_change_boottime=now['boottime'])
    if bound is not None:
        c.require(all(bound[n]==binding[n] for n in ('run_identity','controller_sha256','controller')),'binding_invalid')
        binding=bound.copy()
    return control,binding


def evaluate(directory,policy,bound,now):
    control,binding=record(directory,policy,bound,now)
    terminal=optional(directory,'terminal.json');status=optional(directory,'status.json')
    if terminal is None and ('binding' not in control or 'claim_identity' not in control):
        c.require(bound is None and live(control['owner']),'owner_absent')
        c.require(-1<=now['unix']-c.finite(control['created_unix'])<=c.STALE_SECONDS,'source_stale')
        return c.view('STARTING'),None
    if status is not None:
        c.require(status['owner']==control['owner'] and status['package_manifest_sha256']==policy['gateway_manifest_sha256'] and
            status['state'] in c.STATES[1:-1] and status['starts'] in (0,1) and status['restarts']==status['replayed_requests']==0,
            'binding_invalid')
        nginx=status['nginx']
        if nginx is not None:
            inspection.owner(nginx);c.require(nginx['boot_id']==now['boot_id'] and binding['nginx'] in (None,nginx),'binding_invalid')
            binding['nginx']=nginx
        if 'tls' in status:
            observed=status['tls'];c.require(observed==tls.observe(policy['material'],c.finite(observed['observed_unix'])),'binding_invalid')
    if terminal is not None:
        c.require(terminal['state'] in ('STOPPED','FAILED') and type(terminal['cleanup_confirmed']) is bool and
            not live(control['owner']) and (binding['nginx'] is None or not live(binding['nginx'])),'owner_absent')
        c.require(status is None or all(terminal.get(k)==v for k,v in status.items()),'binding_invalid')
        c.require(terminal['automatic_reactivation'] is False and terminal['backend_stopped_by_gateway'] is False,'binding_invalid')
        reason='none' if terminal['state']=='STOPPED' else terminal.get('error_code','gateway_failure')
        c.require(reason in c.REASONS,'binding_invalid')
        return c.view(terminal['state'],reason,cleanup=terminal['cleanup_confirmed']),binding
    c.require(live(control['owner']),'owner_absent')
    if status is None:
        c.require(-1<=now['unix']-c.finite(control['created_unix'])<=c.STALE_SECONDS,'source_stale')
        return c.view('STARTING'),binding
    digest=sha(directory.read('status.json'))
    # A changed status is a new observation only if the bytes we parsed are
    # still the same. Mixed reads are unknown for this poll, never rebound.
    c.require(parse(directory.read('status.json'))==status,'source_unavailable')
    if digest!=binding['last_status_sha256']:
        binding.update(last_status_sha256=digest,last_change_boottime=now['boottime'])
    age=now['unix']-c.finite(status['updated_unix']);elapsed=now['boottime']-c.finite(binding['last_change_boottime'])
    maximum=60 if status['state']=='STOPPING' else c.STALE_SECONDS
    c.require(-1<=age<=maximum and 0<=elapsed<=maximum,'source_stale')
    age=max(0,age,elapsed);state=status['state']
    c.require(state in ('STARTING','READY','STOPPING'),'binding_invalid')
    if state=='READY':
        c.require('tls' in status and binding['nginx'] is not None and live(binding['nginx']) and
            inspection.process_row(binding['nginx']['pid'])['ppid']==control['owner']['pid'],'owner_absent')
        listener=m.listener(binding['nginx'],policy['listen_host'],policy['listen_port'],policy['nginx_sha256'])
        c.require(status['listener']==listener and binding['listener'] in (None,listener),'binding_invalid')
        binding['listener']=listener
    c.require(sha(directory.read('controller.json'))==binding['controller_sha256'],'binding_invalid')
    return c.view(state,source_age=age),binding


def observe(policy,bound,now=None):
    now=clock() if now is None else now;material=None;new=bound
    try:
        material=material_current(policy)
    except (OSError,ValueError,KeyError,TypeError,RuntimeError,IndexError):
        value=c.view('UNKNOWN','package_unavailable')
    else:
        try:
            path=Path(policy['gateway_run'])
            if not os.path.lexists(path):
                c.require(bound is None,'source_unavailable');value=c.view('WAITING')
            else:
                with Directory(path) as directory:
                    try:control,binding=record(directory,policy,bound,now)
                    except FileNotFoundError:
                        c.require(bound is None,'source_unavailable');value=c.view('STARTING')
                    else:
                        # Initial controller write precedes complete binding.
                        # Pin only its completed record or a terminal failure.
                        if ('binding' in control and 'claim_identity' in control) or optional(directory,'terminal.json') is not None:
                            new=binding
                        value,new=evaluate(directory,policy,new,now)
                    directory.consistent()
        except c.Invalid as error:value=c.view('UNKNOWN',str(error))
        except (OSError,ValueError,KeyError,TypeError,RuntimeError,IndexError):value=c.view('UNKNOWN','source_unavailable')
    value.update(certificate=c.certificate(material,now['unix']),evaluated_unix=now['unix'],
                 evaluated_boottime=now['boottime'],boot_id=now['boot_id'])
    return value,new
