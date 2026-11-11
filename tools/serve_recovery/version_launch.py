"""Build frozen native-root payload commands and expose version registration.

Commands refer to verified retained packages and distinct private run paths.
No command here acquires a GPU lease, starts a model, or resets a journal.
"""
from pathlib import Path

from tools.serve_host import native
from tools.serve_recovery import access_store, bundle, control, ledger
from tools.serve_recovery import version_contract, version_policy, version_registration
from tools.serve_service import package as engine


def configuration(state, root, run):
    selected = version_registration.load(state,root)
    if selected is None:return None
    ledger.require(selected['state']['terminal_fault'] is None,
                   'version recovery exhausted; explicit changed repair required')
    registration = selected['registration']
    root_package = Path(root['package'])
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


def status(state, root):
    if not version_registration.present(state):
        return dict(enabled=False,model_actions=False,automatic_start=False)
    try:
        live = control.request(root['service_id'],'status')
    except OSError:
        live = None
    if live is not None:
        value = live.get('version_recovery')
        ledger.require(type(value) is dict and
            value.get('root_manifest_sha256') == root['manifest_sha256'],
            'live root has no matching version projection')
        return dict(value,enabled=True,live_control=True)
    selected = version_registration.load(state,root)
    value = selected['state'];name = version_policy.launch_payload(value)
    return dict(version_policy.describe(value),enabled=True,live_control=False,
        root_manifest_sha256=root['manifest_sha256'],
        payload_manifest_sha256=value['registry']['payloads'][name]['manifest_sha256'],
        last_fault=value['terminal_fault'] or (value['faults'][-1] if value['faults'] else None))
