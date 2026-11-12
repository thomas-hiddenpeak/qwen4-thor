"""Explicit bounded Guardian supervision, preserving original service intent.

Only actual root exit enters tree cleanup. Auxiliary observation/journal errors
never terminate a surviving core. New observers adopt health monitoring only;
retirement still needs original tree custody, old-boot proof or a resource domain.
"""
from contextlib import ExitStack
from copy import deepcopy
import os
from pathlib import Path
import signal
import time
import uuid

from tools.serve_recovery import bundle, cold_admission, cold_client, control, deployment, ledger, lifecycle
from tools.serve_recovery import outer_control, outer_instance, outer_manager, outer_observe
from tools.serve_recovery import outer_policy, outer_reconcile, outer_store, outer_tree, service, store
from tools.serve_recovery import resource_launch


def selected(state, package, digest):
    """Respect explicit policy; incomplete/lost initialization never downgrades."""
    state = Path(state).absolute()
    with deployment.claim(state, shared=True):
        desc = lifecycle.selected_state(state, package, digest)
        present = any(os.path.lexists(state/name) for name in (outer_manager.INTENT,
            outer_manager.PENDING, outer_store.Store.marker_name, outer_store.Store.directory_name))
        requested = any(name in os.environ for name in (resource_launch.DIRECTORY, resource_launch.ENTRY_FD))
        if not present and not requested: return False
        outer_manager.ready(state, desc)
        policy = lifecycle.read_family(state, desc, outer_store.Store)
        return policy['enabled'] or requested


class Runtime:
    def __init__(self, state, package, digest, manifest, desc, binding, startup=None):
        self.state, self.package, self.digest = state, package, digest
        self.manifest, self.desc, self.binding = manifest, desc, binding
        self.instance = self.tree = self.observer = None
        self.stop_requested = self.stop_sent = False
        self.next_heartbeat = 0
        self.attached = False
        self.phase = 'VERIFYING'
        self.reason = None
        self.last_guardian = None
        self.stop_durable = self.cleaned = False
        self.startup = startup
        self.resource = resource_launch.inherited(desc['service_id'])
        try:
            self.cold = cold_client.inherited(state, digest, desc['service_id'], self.resource)
            self.server = outer_control.Server(desc['service_id'], self.client)
        except BaseException:
            if self.resource is not None: self.resource.close()
            raise

    def status(self):
        value = dict(state=self.phase, reason=self.reason, service_id=self.desc['service_id'],
            business_ready=False, stop_requested=self.stop_requested,
            stop_durable=self.stop_durable, tree_cleanup_verified=self.cleaned,
            cleanup_confirmed=self.cleaned and (not self.stop_requested or self.stop_durable),
            outer_supervised=True, live_control=True)
        if self.last_guardian is not None:
            value.update(guardian=self.last_guardian,
                business_ready=self.last_guardian.get('business_ready') is True,
                stop_durable=self.last_guardian.get('stop_durable') is True or self.stop_durable)
        if self.stop_requested: value['business_ready'] = False
        return value

    def request_stop(self, *_args): self.stop_requested = True

    def client(self, operation):
        if operation == 'stop':
            self.request_stop()
            self.stop_core()
        return self.status()

    def stop_core(self):
        if not self.stop_requested: return
        if self.tree is not None and self.tree.root is not None and self.tree.poll() is None:
            if not self.stop_sent:
                # Explicit operator intent only; this is our unreaped child.
                self.tree.root.send_signal(signal.SIGTERM); self.stop_sent = True
            return
        if self.observer is not None and not self.observer.exited():
            try: self.last_guardian = control.request(self.desc['service_id'], 'stop', timeout=.2)
            except (OSError, ValueError): pass
            return
        if self.instance is None: return
        self.instance.check()
        try:
            with store.Store(self.state, self.desc['service_id']) as current:
                ledger.require(current.service_contract, 'outer stop requires original service state')
                if current.state['desired'] != 'STOPPED':
                    current.transact(dict(id=str(uuid.uuid4()), expected_revision=current.state['revision'],
                        operation='desired', args=dict(state='STOPPED')))
                self.stop_durable = True
        except BlockingIOError:
            # A still-owned writer may be finishing cleanup. No new root can
            # pass its writer lock and the original instance custody meanwhile.
            self.reason = 'stop_waiting_for_writer'

    def heartbeat(self):
        if time.monotonic() < self.next_heartbeat: return
        self.next_heartbeat = time.monotonic()+.5
        if self.observer is not None:
            value = self.observer.sample(adopt=not self.attached)
            self.attached = value['state'] == 'ATTACHED'
            self.phase = value['state']
            self.last_guardian = value.get('guardian')
            return
        if self.tree is None or self.tree.root is None or self.tree.poll() is not None: return
        try:
            value = control.outer_observation(self.desc['service_id'], adopt=not self.attached, timeout=.2)
            ledger.require(value['guardian_pid'] == self.tree.root_identity['pid'] and
                value['guardian_start_ticks'] == self.tree.root_identity['start_ticks'] and
                value.get('outer_observation_accepted') is True,
                'outer live root identity changed')
            self.attached = True; self.last_guardian = value; self.phase = 'ATTACHED'
        except (OSError, ValueError, KeyError, TypeError):
            self.phase = 'UNKNOWN'; self.last_guardian = None
            # Observation failure is never a root exit or a signal reason.

    def tick(self):
        self.server.tick(); self.stop_core(); self.heartbeat()
        if self.startup is not None:
            try: self.startup(self.status())
            except (OSError, ValueError, KeyError, TypeError): self.startup = None

    def unstarted(self):
        """Cancel only this still-held local intent before a successful fork."""
        tree = self.tree
        if tree is None or tree.attempt is None: return
        self.instance.check(); tree.local()
        ledger.require(tree.entered and tree.root is None and outer_tree.no_children() and
            tree.instance is self.instance and tree.instance_custody == self.instance.custody(),
            'unstarted attempt lost its actual custody')
        with outer_store.open_bound(self.binding) as outer:
            previous = outer.head['state']['policy']; attempt = previous['pending']
            ledger.require((attempt == tree.attempt or attempt is None and
                previous['total'] in (tree.attempt['serial']-1, tree.attempt['serial'])) and
                tree.attempt['owner'] is None and
                tree.attempt['custody'] == tree.binding(self.instance), 'unstarted attempt changed')
            original = tree.attempt
            with store.Store(self.state, self.desc['service_id']) as core:
                ledger.require(core.service_contract and not any(core.state['active'].values()) and
                    core.state['generations'] == original['core_generations'],
                    'unstarted attempt has unexplained core slots')
                outer_reconcile.core_successor(core.state, original)
                if attempt is None: return
                candidate = outer_policy.change(previous, 'retired',
                    dict(ticket=attempt['ticket'], verified=True), store.stamp())
                outer.transact(str(uuid.uuid4()), previous['revision'], candidate)

    def reserve(self, outer, core):
        previous = outer.head['state']['policy']
        args = dict(core=core.state, ticket=str(uuid.uuid4()), manifest_sha256=self.digest,
            cleanup_verified=True, identity_valid=True, custody=self.tree.binding(self.instance))
        if self.resource is not None: args['resource'] = self.resource.binding()
        candidate = outer_policy.change(previous, 'request', args, store.stamp())
        result = candidate['last']['result']
        if result['allowed']: self.tree.reserve(self.instance, candidate['pending'])
        if result['allowed'] or candidate['protected'] != previous['protected']:
            outer.transact(str(uuid.uuid4()), previous['revision'], candidate)
        if not result['allowed']:
            self.reason = result['reason']; self.phase = 'WAITING'; return None
        return candidate

    def prepare_cold(self, lease):
        """The issuer reads released journals; it cannot race a held writer."""
        self.tick()
        if self.stop_requested: return False, None, ()
        try: available = self.cold.available()
        except (OSError, ValueError, KeyError, TypeError): available = False
        if not available:
            self.phase = 'WAITING'; self.reason = 'cold_issuer_unavailable'
            return False, None, ()
        self.instance.check(); outer_manager.ready(self.state, self.desc)
        with outer_store.open_bound(self.binding) as outer, store.Store(self.state, self.desc['service_id']) as core:
            ledger.require(core.service_contract, 'cold outer requires original core state')
            candidate = self.reserve(outer, core)
            if candidate is None: return False, None, ()
            attempt = candidate['pending']
        def tick():
            self.tick()
            if self.stop_requested: raise InterruptedError('operator stopped during cold admission')
        try:
            with self.cold.request(attempt, lease, self.instance.fd, tick=tick) as reply:
                config, fds = cold_admission.configuration(self.desc['service_id'], self.digest,
                                                           lease, descriptors=reply.fds)
                return True, config, fds
        except (OSError, ValueError, KeyError, TypeError):
            self.phase = 'WAITING'; self.reason = 'cold_admission_unavailable'
            return False, None, ()

    def dispatch(self, command, *, pass_fds, stdio):
        self.tick()
        if self.stop_requested: return None
        self.instance.check(); outer_manager.ready(self.state, self.desc)
        with outer_store.open_bound(self.binding) as outer:
            with store.Store(self.state, self.desc['service_id']) as core:
                ledger.require(core.service_contract, 'outer dispatch requires original service state')
                if self.tree.attempt is None:
                    ledger.require(self.cold is None, 'cold dispatch requires its prepared original attempt')
                    candidate = self.reserve(outer, core)
                    if candidate is None: return None
                else:
                    candidate = outer.head['state']['policy']
                    ledger.require(self.cold is not None and candidate['pending'] == self.tree.attempt and
                        candidate['pending']['owner'] is None, 'cold prepared outer attempt changed')
                    self.cold.check()
                    if core.state['desired'] != 'RUNNING' or not candidate['enabled'] or \
                            candidate['protected'] or core.state['quarantine'] is not None:
                        self.phase = 'WAITING'; self.reason = 'cold_dispatch_revoked'; return None
                    ledger.require(ledger.digest(core.state) == candidate['pending']['core_sha256'],
                                   'core changed after cold credential issuance')
                attempt = candidate['pending']
                if self.resource is not None:
                    command = self.resource.dispatch(command, self.manifest['python']['path'],
                        self.package/'source/tools/serve_recovery/resource_launch.py')
                    pass_fds = (*pass_fds, self.resource.fd)
                owner = self.tree.launch(command, env=dict(os.environ),
                    pass_fds=(*pass_fds, self.instance.fd), instance=self.instance,
                    attempt=attempt, stdio=stdio)
                # If this write fails after fork, leave the healthy root alive.
                # Its local original Tree still proves ownership on actual exit.
                try:
                    spawned = outer_policy.change(candidate, 'spawned',
                        dict(ticket=attempt['ticket'], owner=owner), store.stamp())
                    outer.transact(str(uuid.uuid4()), candidate['revision'], spawned)
                except (OSError, ValueError):
                    self.reason = 'spawn_receipt_unknown'
                return self.tree.root

    def idle(self, seconds=1):
        end = time.monotonic()+seconds
        while time.monotonic() < end:
            self.tick()
            if self.stop_requested: return
            time.sleep(.05)

    def observe_existing(self):
        self.observer = outer_observe.Observer(self.state, self.package, self.digest)
        try:
            while not self.observer.exited():
                self.tick(); time.sleep(.1)
        finally:
            self.observer.close(); self.observer = None
        self.last_guardian = None; self.attached = False
        # A pinned root exit alone cannot retire its surviving children.

    def supervise(self, *, activate=False):
        while self.instance is None:
            candidate = outer_instance.Instance(self.state, self.desc['service_id'])
            try: self.instance = candidate.__enter__()
            except BlockingIOError: self.observe_existing()
        try:
            policy = lifecycle.read_family(self.state, self.desc, outer_store.Store)
            if policy['pending'] is not None:
                # A surviving old journal can hold the writer. The original
                # resource path must retire it before opening that writer.
                if self.resource is not None and policy['pending']['boot_id'] == store.stamp().boot_id:
                    from tools.serve_recovery import resource_reconcile
                    resource_reconcile.retire(self.instance, self.resource)
                else: outer_reconcile.reboot(self.instance, resource=self.resource)
            with store.Store(self.state, self.desc['service_id']) as core:
                ledger.require(core.service_contract, 'outer runtime requires core service fence')
                if activate and not self.stop_requested:
                    policy = lifecycle.read_family(self.state, self.desc, outer_store.Store)
                    ledger.require(policy['enabled'] and not policy['protected'] and
                        policy['pending'] is None and not any(core.state['active'].values()) and
                        core.state['quarantine'] is None, 'explicit activation requires reconciled unprotected state')
                    if core.state['desired'] != 'RUNNING':
                        core.transact(dict(id=str(uuid.uuid4()), expected_revision=core.state['revision'],
                            operation='desired', args=dict(state='RUNNING')))
            while True:
                self.tick()
                try:
                    with store.Store(self.state, self.desc['service_id']) as core:
                        current = deepcopy(core.state)
                except BlockingIOError:
                    self.phase = 'WAITING'; self.reason = 'core_writer_busy'; self.idle(); continue
                policy = lifecycle.read_family(self.state, self.desc, outer_store.Store)
                ledger.require(not any(current['active'].values()) and policy['pending'] is None,
                               'outer restart requires independently reconciled old tree')
                self.cleaned = True; self.stop_durable = current['desired'] == 'STOPPED'
                if current['desired'] != 'RUNNING': self.phase = 'STOPPED'; return self.status()
                if not policy['enabled']: self.phase = 'DISABLED'; return self.status()
                if current['quarantine'] is not None or policy['protected']:
                    self.phase = 'PROTECTED'; return self.status()
                preview = deepcopy(policy); now = outer_policy.tick(preview, store.stamp())
                if preview['starts'] and now-preview['starts'][-1]['at'] < outer_policy.COOLDOWN:
                    self.phase = 'WAITING'; self.reason = 'cooldown'; self.idle(); continue
                self.tree = outer_tree.Tree().__enter__()
                self.attached = self.stop_sent = False; self.last_guardian = None
                self.cleaned = False; self.reason = None
                try:
                    service.launch_owned(self.package, self.digest, self.state, manifest=self.manifest,
                                         resume=True, outer=self)
                    if self.tree.root is not None:
                        ledger.require(self.tree.poll() is not None, 'outer returned before actual root exit')
                        self.phase = 'DRAINING'; self.last_guardian = None
                        self.tree.drain(tick=self.tick)
                        outer_reconcile.continuous(self.instance, self.tree)
                    else: self.unstarted()
                except BlockingIOError:
                    if self.tree.root is not None: raise
                    self.unstarted(); self.phase = 'WAITING'; self.reason = 'authority_busy'
                except BaseException:
                    if self.tree.root is None: self.unstarted()
                    # Never convert an auxiliary failure to user STOPPED.
                    raise
                finally:
                    if outer_tree.no_children(): self.tree.close()
                self.tree = None; self.cleaned = True
                self.idle()
        finally:
            self.instance.close(); self.instance = None

    def close(self):
        self.server.close()
        if self.resource is not None: self.resource.close()


def run(state, package, digest, *, activate=False, startup=None):
    state = Path(state).absolute(); package = Path(package).resolve(strict=True)
    manifest = bundle.verify(package, digest)
    ledger.require(manifest['version'] >= 18 and type(activate) is bool,
                   'explicit outer runtime requires profile v18')
    with ExitStack() as locks:
        locks.enter_context(deployment.claim(state, shared=True))
        locks.enter_context(deployment.claim(state, name='outer-runtime.lock'))
        desc = lifecycle.selected_state(state, package, digest)
        binding = outer_manager.ready(state, desc)
        runtime = Runtime(state, package, digest, manifest, desc, binding, startup=startup)
        previous = {}
        try:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[sig] = signal.signal(sig, runtime.request_stop)
            return runtime.supervise(activate=activate)
        finally:
            for sig, handler in previous.items(): signal.signal(sig, handler)
            runtime.close()
