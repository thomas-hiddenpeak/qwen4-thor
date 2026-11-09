"""Coordinated native default publication and explicit interrupted-write recovery.

The immutable request precedes both journals. Model ownership is held through
the final receipt; publishing never starts, stops, or resets a model/service.
"""
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path

from tools.serve_host import native
from tools.serve_recovery import access_store, bundle, deployment, ledger, lifecycle, store
from tools.serve_recovery import publication_files as files, publication_policy as policy
from tools.serve_recovery import publication_journal as journal
from tools.serve_service import package as engine

INTENTS = 'service-publication-intents'
MAX_INTENTS = 128


class Store(access_store.Store):
    contract = policy
    format = 'q4t-service-publication-store-v1'
    marker_name = 'service-publication.json'
    binding_format = 'q4t-service-publication-binding-v1'
    directory_name = 'service-publication'


def present(state):
    return any(os.path.lexists(Path(state)/name) for name in
               (Store.marker_name, Store.directory_name, INTENTS))


@contextmanager
def quiescent(state, desc):
    with ExitStack() as locks:
        locks.enter_context(deployment.claim(state))
        locks.enter_context(deployment.claim(state, name='guardian.lock'))
        core = locks.enter_context(store.Store(state,desc['service_id']))
        ledger.require(deployment.read_descriptor(state) == desc, 'publication service changed')
        observed = native.read_ledger(dict(state=str(state), descriptor=desc))
        ledger.require(observed['desired'] == 'STOPPED' and not observed['active'] and
            core.state['desired'] == 'STOPPED' and not any(core.state['active'].values()),
                       'publication requires durable STOPPED and complete cleanup')
        yield core


def intent_path(state, request_id):
    ledger.require(ledger.identifier(request_id), 'invalid publication request id')
    return Path(state)/INTENTS/(request_id+'.json')


def validate_intent(value, state, desc):
    ledger.keys(value, ('version', 'format', 'request_id', 'binding', 'base',
                        'target', 'rollback', 'deployment'))
    ledger.require(type(value['version']) is int and value['version'] == 1 and
        value['format'] == 'q4t-publication-intent-v1' and
        ledger.identifier(value['request_id']) and type(value['rollback']) is bool and
        value['binding'] == access_store.binding(state, desc['service_id']),
        'publication intent identity differs')
    base = policy.validate(value['base']);target = policy.snapshot(value['target'])
    previous = deployment.policy.descriptor(value['deployment'])
    ledger.require(previous['service_id'] == desc['service_id'] and
        previous['package'] == base['current']['package'] and
        previous['manifest_sha256'] == base['current']['manifest_sha256'],
        'publication deployment predecessor differs')
    policy.prepare(base, target, value['request_id'], value['rollback'])
    return value


def load_intent(state, desc, request_id):
    path = intent_path(state, request_id)
    files.directory(path.parent)
    value = validate_intent(native.private_json(path, policy.MAX_BYTES), state, desc)
    ledger.require(value['request_id'] == request_id, 'publication request filename differs')
    return value


def save_intent(state, desc, value):
    validate_intent(value, state, desc)
    path = intent_path(state, value['request_id'])
    files.directory(path.parent, create=True);files.sync(state)
    names = list(path.parent.iterdir())
    ledger.require(path.exists() or path.with_name(path.name+'.new').exists() or
                   len(names) < MAX_INTENTS, 'publication intent capacity reached')
    files.immutable_json(path, value)


def prepare_intent(state, desc, package, digest, release, root, expected, revision,
                   request_id, generation=None):
    ledger.require(policy.revision(revision) and type(expected) is str and
                   ledger.HASH.fullmatch(expected), 'publication predecessor required')
    root = files.directory(root)
    if os.path.lexists(state/Store.marker_name):
        base = lifecycle.read_family(state, desc, Store)
        ready(state, desc)
        ledger.require(base['root'] == str(root), 'publication root changed')
    else:
        ledger.require(not os.path.lexists(state/Store.directory_name),
                       'unpublished initialization needs its original request')
        if os.path.lexists(state/INTENTS):
            files.directory(state/INTENTS)
            ledger.require({item.name for item in (state/INTENTS).iterdir()} <=
                {request_id+'.json.new'}, 'unpublished intent needs its original request')
        if os.path.lexists(state/deployment.Store.marker_name):
            selected = lifecycle.read_family(state,desc,deployment.Store)
            ledger.require(selected['phase'] == 'stable' and selected['current'] == desc,
                           'publication predecessor deployment needs resume')
        else:ledger.require(desc['version'] == 1, 'deployment journal unavailable')
        missing = [not os.path.lexists(root/name) for name in files.ALIASES]
        ledger.require(all(missing) or not any(missing), 'partial legacy default requires recovery')
        fresh = all(missing)
        ledger.require(not fresh or (str(Path(package).resolve()) == desc['package'] and
            digest == desc['manifest_sha256']), 'fresh default must match initialized service')
        current = files.snapshot(root, state, desc, desc['package'], desc['manifest_sha256'],
                                 release if fresh else root/'service.release.json')
        if not fresh:
            ledger.require((root/'service').is_symlink() and
                (root/'service').resolve() == Path(desc['package']) and
                all((root/name).resolve() == root/name and engine.sha(root/name) ==
                    engine.sha(files.generation(root,current)/name)
                    for name in ('q4t','q4t.release.json','service.release.json')),
                'legacy default identity differs')
        base = policy.initial(root, current, fresh=fresh)
    ledger.require(base['phase'] in ('unpublished','stable') and base['revision'] == revision and
        desc['manifest_sha256'] == expected and base['current']['package'] == desc['package'] and
        base['current']['manifest_sha256'] == expected, 'publication predecessor changed')
    if generation is None:
        target = files.snapshot(root, state, desc, package, digest, release)
    else:
        ledger.require(type(generation) is str and ledger.HASH.fullmatch(generation),
                       'invalid rollback generation')
        targets = [s for h in base['history'] for s in (h['previous'],h['selected'])
                   if s['generation_sha256'] == generation]
        ledger.require(bool(targets) and all(s == targets[0] for s in targets),
                       'rollback generation is not retained publication history')
        target = targets[0];files.verify(root, state, desc, target)
    deployment.check_target(desc, dict(desc, package=target['package'],
                                      manifest_sha256=target['manifest_sha256']))
    value = dict(version=1, format='q4t-publication-intent-v1', request_id=request_id,
        binding=access_store.binding(state,desc['service_id']), base=base, target=target,
        rollback=generation is not None, deployment=desc)
    save_intent(state, desc, value)
    return value


def target_descriptor(intent):
    previous = intent['deployment'];target = intent['target']
    return dict(version=2, service_id=previous['service_id'], package=target['package'],
        manifest_sha256=target['manifest_sha256'],
        deployment_revision=previous.get('deployment_revision',0)+1,
        deployment_id=intent['request_id'])


def selected_files(state, desc, value):
    root = Path(value['root'])
    files.verify(root,state,desc,value['current'])
    ledger.require(files.selected(root) == value['current']['generation_sha256'] and files.aliases(root),
                   'default generation is not durably selected')


def ready(state, desc):
    current = lifecycle.read_family(state, desc, Store)
    ledger.require(current['phase'] == 'stable' and
        current['current']['package'] == desc['package'] and
        current['current']['manifest_sha256'] == desc['manifest_sha256'],
        'default publication needs resume')
    selected_files(state, desc, current)
    return current


def resume_owned(state, desc, intent, core):
    binding = intent['binding'];base = intent['base'];target = intent['target']
    request = intent['request_id'];root = Path(base['root'])
    prepared = policy.prepare(base, target, request, intent['rollback'])
    finished = policy.finish(prepared);target_desc = target_descriptor(intent)
    files.verify(root,state,desc,base['current']);files.verify(root,state,desc,target)

    def selected_deployment():
        selected = deployment.read_descriptor(state)
        current = lifecycle.read_family(state, selected, deployment.Store)
        ledger.require(selected == target_desc and current['phase'] == 'stable' and
            current['current'] == target_desc, 'publication finish lacks deployment commit')
        selected_files(state,selected,finished)

    def publication_candidate(previous):
        if previous == base:return prepared, journal.transaction_id(request,'prepare')
        ledger.require(previous == prepared, 'publication resume predecessor differs')
        selected_deployment()
        return finished, journal.transaction_id(request,'finish')

    def deployment_candidate(previous):
        if previous['phase'] == 'stable':
            ledger.require(previous['current'] == intent['deployment'],
                           'deployment prepare predecessor differs from intent')
            old, _ = deployment.check_target(previous['current'], target_desc)
            candidate = deployment.policy.prepare(previous, target_desc, request,
                                                   deployment.seeds(state,old))
            return candidate, journal.transaction_id(request,'prepare')
        ledger.require(previous['current'] == intent['deployment'] and
            previous['target'] == target_desc and previous['request'] == request,
            'deployment finish differs from publication intent')
        # A finish is written only after descriptor + default selection. Prove
        # that prerequisite again before accepting even a complete pending file.
        ledger.require(deployment.read_descriptor(state) == target_desc,
                       'deployment finish lacks selected descriptor')
        selected_files(state, target_desc, finished)
        return deployment.policy.finish(previous), journal.transaction_id(request,'finish')

    recovery = journal.recovering(Store, publication_candidate)
    ledger.require(os.path.lexists(state/Store.marker_name), 'publication bootstrap unavailable')
    with access_store.open_bound(binding, recovery) as current:
        value = current.head['state']['policy']
        if value == finished:
            selected_deployment()
            return dict(policy.describe(value), published=True, replayed=True, durable=True)
        if value == base:
            current.transact(journal.transaction_id(request,'prepare'),base['revision'],prepared)
        else:ledger.require(value == prepared, 'publication request no longer current')

        def select(previous, selected):
            ledger.require(previous == intent['deployment'] and selected == target_desc,
                           'deployment callback differs from publication intent')
            files.publish(root,state,selected,base['current'],target,fresh=base['fresh'])

        deployment.switch(state,target['package'],target['manifest_sha256'],
            expected=intent['deployment']['manifest_sha256'],request_id=request,
            rollback=intent['rollback'],_publication=select,
            _writer=journal.recovering(deployment.Store,deployment_candidate),_locked_core=core)
        selected_deployment()
        current.transact(journal.transaction_id(request,'finish'),prepared['revision'],finished)
        return dict(policy.describe(finished),published=True,replayed=False,durable=True)


def execute(state, *, request_id, root=None, package=None, digest=None, release=None,
            expected=None, revision=None, generation=None, resume=False):
    """STOPPED-only transaction; the inherited GPU lease is never unlocked."""
    from tools.serve_recovery import service
    state = Path(state).absolute()
    ledger.require(ledger.identifier(request_id), 'invalid publication request id')
    with ExitStack() as locks:
        locks.enter_context(deployment.claim(state,name='publication.lock'))
        desc = deployment.read_descriptor(state)
        manifest = bundle.verify(desc['package'],desc['manifest_sha256'])
        lease = service.acquire_lease(manifest['lock_path']);locks.callback(os.close,lease)
        with quiescent(state,desc) as core:
            if os.path.lexists(intent_path(state,request_id)):
                intent = load_intent(state,desc,request_id)
                if not resume:
                    ledger.require(intent['base']['root'] == str(Path(root).absolute()) and
                        intent['base']['revision'] == revision and
                        intent['deployment']['manifest_sha256'] == expected and
                        intent['rollback'] == (generation is not None),
                        'publication idempotency conflict')
                    if generation is None:
                        ledger.require(intent['target']['package'] == str(Path(package).resolve()) and
                            intent['target']['manifest_sha256'] == digest and
                            intent['target']['service_release_sha256'] == engine.sha(release),
                            'publication idempotency target conflict')
                    else:ledger.require(intent['target']['generation_sha256'] == generation,
                                        'publication rollback idempotency conflict')
            else:
                ledger.require(not resume, 'publication request intent unavailable')
                intent = prepare_intent(state,desc,package,digest,release,root,expected,
                                        revision,request_id,generation)
            # Bootstrap may need to finish an interrupted initial marker. It is
            # only legal under all ownership locks, before any prepared publish.
            if not os.path.lexists(state/Store.marker_name):
                ledger.require(intent['base']['phase'] == 'unpublished' and
                    desc == intent['deployment'] and files.selected(intent['base']['root']) is None,
                               'published journal cannot be recreated')
                access_store.bootstrap_bound(intent['binding'],intent['base'],Store)
            return resume_owned(state,desc,intent,core)


def status(state):
    state = Path(state).absolute();desc = deployment.read_descriptor(state)
    if not present(state):
        return dict(status='UNPUBLISHED',ready=False,model_actions=False,automatic_start=False)
    with deployment.claim(state,shared=True,name='publication.lock',create=False):
        requests = []
        if (state/INTENTS).exists():
            files.directory(state/INTENTS)
            requests = sorted(p.stem for p in (state/INTENTS).iterdir()
                              if p.suffix == '.json' and ledger.identifier(p.stem))
        result = dict(status='UNKNOWN',ready=False,request_ids=requests,
                      model_actions=False,automatic_start=False)
        try:
            value = lifecycle.read_family(state,desc,Store)
            result.update(policy.describe(value))
            ready(state,desc)
            current = lifecycle.read_family(state,desc,deployment.Store)
            ledger.require(current['phase'] == 'stable' and current['current'] == desc,
                           'publication deployment needs resume')
            result.update(status='PASS',ready=True)
        except (OSError,ValueError,RuntimeError,KeyError,TypeError) as error:
            result['category'] = type(error).__name__
        return result
