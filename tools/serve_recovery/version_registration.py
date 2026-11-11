"""Explicit stopped version-cycle registration with a preserved predecessor.

Only this operator transaction can start a new repair cycle. Runtime workers
cannot create families, replace registration, or erase an exhausted cycle.
Each immutable intent retains the previous outcome and exact retry candidate.
"""
import copy
from contextlib import ExitStack
import os
from pathlib import Path

from tools.serve_host import native
from tools.serve_recovery import access_store, deployment, ledger, publication_files
from tools.serve_recovery import store, version_contract, version_policy, version_store

DIRECTORY = 'version-registrations'
FORMAT = 'q4t-version-registration-intent-v1'
MAX_CYCLES = 16
MAX_BYTES = 64*1024


def present(state):
    return any(os.path.lexists(Path(state)/name) for name in
               (DIRECTORY, version_store.Store.marker_name, version_store.Store.directory_name))


def intents(state):
    directory = Path(state)/DIRECTORY
    publication_files.directory(directory)
    ledger.require(directory.stat().st_mode & 0o777 == 0o700,
                   'private version registration directory required')
    entries = list(directory.iterdir())
    ledger.require(len(entries) <= MAX_CYCLES*2, 'version registration count bound')
    result = {}
    for path in entries:
        suffix = '.json.new' if path.name.endswith('.json.new') else '.json'
        identifier = path.name[:-len(suffix)]
        ledger.require(path.name == identifier+suffix and ledger.identifier(identifier),
                       'unexpected version registration entry')
        result.setdefault(identifier, {})['pending' if suffix.endswith('.new') else 'committed'] = path
    ledger.require(len(result) <= MAX_CYCLES, 'version registration cycle bound')
    return result


def intent(value, binding):
    ledger.keys(value, ('format', 'binding', 'registration', 'previous', 'candidate'))
    ledger.require(value['format'] == FORMAT and value['binding'] == binding,
                   'version registration intent identity differs')
    registration = version_contract.verify(value['registration'])
    previous = value['previous']
    expected = version_policy.initial(registration['registry'], registration['bootstrap_boot_id'])
    if previous is not None:
        version_policy.validate(previous)
        ledger.require(previous['registry']['cycle_id'] != registration['registry']['cycle_id'] and
            (previous['registry']['root_manifest_sha256'] !=
             registration['registry']['root_manifest_sha256'] or
             previous['registry']['payloads']['primary'] !=
             registration['registry']['payloads']['primary']),
            'new recovery cycle requires an explicit changed repair deployment')
        expected['revision'] = previous['revision']+1
    ledger.require(value['candidate'] == expected and
                   len(ledger.canonical(value)) <= MAX_BYTES,
                   'version registration candidate changed')
    return value


def install(state, registration, *, request_id, expected_cycle):
    version_contract.verify(registration)
    ledger.require(request_id == registration['registry']['cycle_id'] and
        (expected_cycle is None or ledger.identifier(expected_cycle)),
        'version registration request identity')
    state = Path(state).absolute()
    with ExitStack() as locked:
        locked.enter_context(deployment.claim(state,shared=True,name='publication.lock'))
        locked.enter_context(deployment.claim(state))
        locked.enter_context(deployment.claim(state,name='guardian.lock'))
        desc = deployment.read_descriptor(state)
        deployment.ready(state,desc)
        ledger.require(registration['root'] == dict(package=desc['package'],
            manifest_sha256=desc['manifest_sha256']), 'registration is not the selected control root')
        binding = access_store.binding(state,desc['service_id'])
        core = locked.enter_context(store.Store(state,desc['service_id']))
        ledger.require(core.state['desired'] == 'STOPPED' and not any(core.state['active'].values()),
                       'version registration requires durable stopped authority')
        directory = state/DIRECTORY
        if not os.path.lexists(directory):
            directory.mkdir(mode=0o700);publication_files.sync(state)
        entries = intents(state)
        path = directory/(request_id+'.json')
        if os.path.lexists(path):
            value = intent(native.private_json(path,MAX_BYTES),binding)
            ledger.require(value['registration'] == registration and expected_cycle ==
                (None if value['previous'] is None else value['previous']['registry']['cycle_id']),
                'version registration replay changed')
        else:
            ledger.require(len(entries) < MAX_CYCLES or request_id in entries,
                           'version registration storage is full')
            previous = None
            if os.path.lexists(state/version_store.Store.marker_name):
                with version_store.open_bound(binding) as current:
                    previous = copy.deepcopy(current.head['state']['policy'])
            ledger.require(expected_cycle == (None if previous is None else
                previous['registry']['cycle_id']), 'version cycle predecessor changed')
            for key,files in entries.items():
                if key == request_id:continue
                ledger.require('committed' in files and 'pending' not in files,
                               'another version registration needs resume')
                old = native.private_json(files['committed'],MAX_BYTES)
                ledger.require(previous is not None and old['candidate']['revision'] <=
                    previous['revision'], 'another version registration is not committed')
            candidate = version_policy.initial(registration['registry'],
                                                registration['bootstrap_boot_id'])
            if previous is not None:candidate['revision'] = previous['revision']+1
            value = intent(dict(format=FORMAT,binding=binding,registration=registration,
                                previous=previous,candidate=candidate),binding)
        # Frozen bootstrap_boot_id makes even a partially written intent
        # reproducible across host boots. No clocks are regenerated on retry.
        publication_files.immutable_json(path,value)
        previous,candidate = value['previous'],value['candidate']
        if previous is None:
            access_store.bootstrap_bound(binding,candidate,version_store.Store)
        else:
            class Contract:
                MAX_BYTES = version_policy.MAX_BYTES
                validate = staticmethod(version_policy.validate)
                digest = staticmethod(version_policy.digest)
                revision = staticmethod(version_policy.revision)

                @staticmethod
                def transition(before,after):
                    if before == previous and after == candidate:return after
                    return version_policy.transition(before,after)

            class AuthorizedStore(version_store.Store):
                contract = Contract

                def reconcile(self):
                    if self.exists('policy.pending') and self.exists('policy.json'):
                        before = self.envelope('policy.json')['state']['policy']
                        pending = self.envelope('policy.pending')['state']
                        if before['registry'] != pending['policy']['registry']:
                            ledger.require(pending['receipts'][-1]['id'] == request_id,
                                           'pending repair belongs to another registration')
                    super().reconcile()

            with access_store.open_bound(binding,AuthorizedStore) as current:
                head = current.head['state']['policy']
                if head['registry'] != candidate['registry']:
                    ledger.require(head == previous, 'version registration was superseded')
                    current.transact(request_id,previous['revision'],candidate)
        with version_store.open_bound(binding) as current:
            actual = copy.deepcopy(current.head['state']['policy'])
            ledger.require(actual['registry'] == candidate['registry'] and
                actual['revision'] >= candidate['revision'],
                           'version registration committed state differs')
        return dict(version_policy.describe(actual), registered=True,
                    intent_sha256=ledger.digest(value), automatic_start=False)


def load(state, root):
    """Explicit launch preparation only; runtime uses its independent worker."""
    state = Path(state).absolute()
    if not present(state):return None
    binding = access_store.binding(state,root['service_id'])
    with version_store.open_bound(binding) as current:
        selected = copy.deepcopy(current.head['state']['policy'])
    entries = intents(state)
    for files in entries.values():
        ledger.require('committed' in files and 'pending' not in files,
                       'version registration needs resume')
        value = native.private_json(files['committed'],MAX_BYTES)
        ledger.require(value['candidate']['revision'] <= selected['revision'],
                       'version registration selection needs resume')
    cycle = selected['registry']['cycle_id']
    ledger.require(cycle in entries,'selected version registration is missing')
    value = intent(native.private_json(entries[cycle]['committed'],MAX_BYTES),binding)
    registration = value['registration']
    ledger.require(registration['registry'] == selected['registry'] and
        registration['root'] == dict(package=root['package'],manifest_sha256=root['manifest_sha256']),
        'selected root needs explicit version registration')
    return dict(registration=registration,state=selected)
