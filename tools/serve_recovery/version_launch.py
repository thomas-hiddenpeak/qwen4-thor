"""Build frozen native-root payload commands and expose version registration.

Commands refer to verified retained packages and distinct private run paths.
No command here acquires a GPU lease, starts a model, or resets a journal.
"""
from pathlib import Path

from tools.serve_host import native
from tools.serve_recovery import access_store, bundle, control, ledger
from tools.serve_recovery import version_contract, version_policy, version_registration
from tools.serve_service import package as engine


def configuration(state, root, run, *, resources=None):
    selected = version_registration.load(state,root)
    if selected is None:return None
    ledger.require(selected['state']['terminal_fault'] is None,
                   'version recovery exhausted; explicit changed repair required')
    registration = selected['registration']
    root_package = Path(root['package'])
    resource_worker = None
    if resources is not None:
        from tools.serve_recovery import resource_launch
        resource_launch.validate(resources)
        manifest = bundle.verify(root_package, root['manifest_sha256'])
        source = 'source/tools/serve_recovery/resource_memory.py'
        ledger.require(source in manifest['files'], 'fixed recovery root lacks resource collector')
        resource_worker = dict(path=str(root_package/source), sha256=manifest['files'][source])
    from tools.serve_recovery import service
    bound = access_store.binding(state,root['service_id'])
    commands = {}
    for name,item in registration['payloads'].items():
        package = Path(item['descriptor']['package'])
        manifest = bundle.verify(package,item['descriptor']['manifest_sha256'])
        directory = Path(run)/name;directory.mkdir(mode=0o700)
        controller = service.controller_configuration(package,
            item['descriptor']['manifest_sha256'],state,directory)
        controller.update(version=5,limits=bound,profiles=bound)
        if resources is not None:
            from tools.serve_recovery import resource_compat
            ledger.require(resource_compat.aware(package),
                           'prepare the retained resource adapter before resource-bound recovery')
            controller.update(version=6, resources=resources, resource_worker=resource_worker)
        engine.save(directory/'controller.json',controller)
        engine.save(directory/'gateway.json',bundle.gateway_config_for(
            package,manifest,directory,access=bound))
        python = manifest['python']['path']
        commands[name] = dict(
            controller=[python,'-I','-B',str(package/'source/tools/serve_recovery/controller.py'),
                        '--config',str(directory/'controller.json')],
            gateway=[python,'-I','-B',str(package/'source/tools/serve_recovery/native_ingress.py'),
                     '--config',str(directory/'gateway.json')],
            binding=item['binding'])
    return dict(registry=registration['registry'],payloads=commands,
        journal=str(root_package/'source/tools/serve_recovery/version_journal.py'),
        cuda_probe=str(root_package/'bin/q4t-cuda-probe'))


def prepare_file(root, fallback, receipt, output, cycle_id):
    registration = version_contract.prepare(root,root,fallback,cycle_id,
                                           fallback_acceptance=receipt)
    path = Path(output).absolute()
    # The frozen file is the exact input on retry, including the original boot.
    from tools.serve_recovery import publication_files
    publication_files.immutable_json(path,registration)
    return dict(registration=str(path),sha256=engine.sha(path),cycle_id=cycle_id,
                automatic_start=False,model_actions=False)


def register_file(state, root, path, expected_cycle):
    registration = native.private_json(path,16*1024)
    ledger.require(registration['root'] == root, 'registration selected root differs')
    return version_registration.install(state,registration,
        request_id=registration['registry']['cycle_id'],expected_cycle=expected_cycle)


def observed(state, root):
    """Read live root or stopped durable selection, without journal mutation."""
    if not version_registration.present(state):
        return None
    try:
        live = control.request(root['service_id'],'status')
    except OSError:
        live = None
    if live is not None:
        bundle.verify(root['package'],root['manifest_sha256'])
        context = {name:native.inode('/proc/self/ns/'+name) for name in ('pid','user','net','mnt')}
        ledger.require(live.get('service_id') == root['service_id'], 'foreign live version service')
        native.process(live['guardian_pid'],live['guardian_start_ticks'],
            native.inode(Path(root['package'])/'bin/q4t-guardian'),context)
        value = native.project_version(live.get('version_recovery'))
        ledger.require(value is not None and value['root_manifest_sha256'] == root['manifest_sha256'],
            'live root has no matching version projection')
        registration = version_registration.for_cycle(state,root,value['cycle_id'])
        ledger.require(value['payload_manifest_sha256'] is None or
            value['payload_manifest_sha256'] in {p['descriptor']['manifest_sha256']
                for p in registration['payloads'].values()}, 'foreign live payload')
        return dict(registration=registration,projection=dict(value,live_control=True))
    from tools.serve_recovery import lifecycle, version_store
    head = lifecycle.read_family(state,root,version_store.Store)
    selected = version_registration.for_state(state,root,head)
    value = selected['state'];name = version_policy.launch_payload(value)
    projection = dict(version_policy.describe(value),enabled=True,live_control=False,
        root_manifest_sha256=root['manifest_sha256'],
        payload_manifest_sha256=value['registry']['payloads'][name]['manifest_sha256'],
        last_fault=value['terminal_fault'] or (value['faults'][-1] if value['faults'] else None))
    return dict(registration=selected['registration'],projection=projection)


def status(state, root):
    value = observed(state,root)
    if value is None:return dict(enabled=False,model_actions=False,automatic_start=False)
    return dict(value['projection'],model_actions=False,automatic_start=False)
