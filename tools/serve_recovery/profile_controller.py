"""Qualified profile coordination; caller alone drains and asks Guardian."""
import copy
import os

from tools.serve_recovery import ledger, policy, profile_contract, profile_memory
from tools.serve_recovery import profile_policy, profile_runtime, resource_memory
from tools.serve_service import supervisor


class Profiles:
    def __init__(self, controller, registry, binding):
        base = controller.base
        expected = profile_contract.binding(base['files']['q4t'],
            base['protocol_sha256'], base['model_files'],
            controller.manifest['model_assets'], base['settings'].get('native_embedding'))
        profile_contract.validate(registry)
        ledger.require(registry['identity'] == expected and
            registry['profiles']['primary'] == base['settings']['profile'],
            'controller qualified capacity differs')
        self.original = copy.deepcopy(base)
        self.registry = registry
        boot = supervisor.process_identity(os.getpid())['boot_id']
        self.selection = profile_runtime.Selection(binding, registry, boot,
            configuration=controller.manifest['recovery_profile_policy'])
        self.detector = None
        self.resource_pressure = resource_memory.Pressure()
        self.launch = None
        self.memory_fault = False
        self.generation = 0

    def step(self, *, allow_start=True):
        self.selection.step(allow_start=allow_start)

    def prepare_launch(self, controller, observation):
        runtime = self.selection
        if self.launch is None:
            runtime.unpark()
            if not runtime.ready():
                controller.reason = runtime.error or 'profile_state_wait'
                return False
            state = runtime.state
            generation = max(self.generation, state['completed_generation'])
            if self.memory_fault and state['selected'] == 'primary' and state['pending'] is None:
                runtime.prepare('light', 'confirmed_memory_fault', generation)
                controller.reason = 'profile_memory_fault_switch_wait'
                return False
            result = profile_memory.cold(self.registry, state, observation)
            if not result['ready']:
                if result['target'] is not None:
                    runtime.prepare(result['target'], result['reason'], generation)
                controller.reason = result['reason']
                return False
            self.launch = result['target']
            controller.base = copy.deepcopy(self.original)
            controller.settings = controller.base['settings']
            controller.settings['profile'] = copy.deepcopy(self.registry['profiles'][self.launch])
        # No helper, even our own storage process, can bypass the Guardian's
        # proof that the previous complete model/auxiliary tree is empty.
        if not runtime.park():
            controller.reason = 'profile_worker_reap'
            return False
        # A valid selection is not a RAM reservation; recheck before every
        # grant, including after an asynchronous helper reap.
        try:
            free = profile_memory.available(observation)
        except (ValueError, KeyError, TypeError):
            controller.reason = 'memory_unknown'
            return False
        if free < self.registry['qualifications'][self.launch]['startup_required_bytes']:
            controller.reason = 'memory_headroom'
            self.launch = None
            return False
        return True

    def started(self, generation):
        self.generation = generation
        self.memory_fault = False
        self.detector = None
        self.resource_pressure.reset()
        self.selection.unpark()

    def fault(self, controller, kind):
        if (kind == policy.FaultKind.RESOURCE_EXHAUSTED and controller.watchdog is not None and
                controller.watchdog.snapshot().get('reason') == 'critical_memory_pressure'):
            self.memory_fault = True

    def switch_ready(self, controller):
        """True only AFTER a qualified intent is durably confirmed."""
        runtime = self.selection
        try:
            if not runtime.ready():
                if controller.phase == 'READY':
                    controller.reason = runtime.error or 'profile_state_wait'
                if self.detector is not None: self.detector.reset()
                self.resource_pressure.reset()
                return False
            state = runtime.state
            pending = state['pending']
            if pending is not None:
                if pending['target'] == self.launch:
                    runtime.confirm(controller.grant['generation'], self.launch)
                    return False
                ledger.require(pending['source'] == self.launch and
                    pending['source_generation'] <= controller.grant['generation'],
                    'pending profile belongs to another model generation')
                return True
            ledger.require(state['selected'] == self.launch,
                           'active model differs from durable profile')
            if self.detector is None:
                self.detector = profile_memory.Detector(self.registry, state['configuration'])
            now = runtime.nanos()
            resources = getattr(controller, 'resource_memory', None)
            observed = None if resources is None else resources.snapshot()
            q = self.registry['qualifications']
            extra = q['primary']['startup_required_bytes']-q['light']['startup_required_bytes']
            recover = resources is None or resource_memory.recovery_clear(observed, now, extra)
            result = self.detector.assess(self.launch, controller.watchdog.snapshot(), now,
                ready=controller.phase == 'READY' and (self.launch != 'light' or recover))
            if self.launch == 'primary' and controller.phase == 'READY':
                if self.resource_pressure.assess(observed, now, state['configuration']['pressure_seconds']):
                    result = dict(target='light', reason='sustained_memory_pressure')
            else: self.resource_pressure.reset()
            if result is not None:
                runtime.prepare(result['target'], result['reason'], controller.grant['generation'])
            if controller.phase == 'READY' and self.launch == 'light':
                controller.reason = 'serving_light'
        except Exception:
            runtime.error = 'profile_state_unavailable'
            if self.detector is not None: self.detector.reset()
            self.resource_pressure.reset()
        return False

    def retired(self):
        self.launch = None
        self.detector = None
        self.resource_pressure.reset()
        self.selection.park()

    def close(self):
        return self.selection.close()
