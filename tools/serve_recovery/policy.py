"""Pure CPU recovery decisions. Observations never stand in for safety proof.

The Guardian validates generation/ownership and supplies the evidence. This
module neither probes hardware nor signals a process. A restart decision still
requires a durable ledger grant and successful old-tree cleanup.
"""
from dataclasses import dataclass
from enum import StrEnum


class Desired(StrEnum):
    RUNNING = 'RUNNING'
    STOPPED = 'STOPPED'
    MAINTENANCE = 'MAINTENANCE'


class FaultKind(StrEnum):
    REQUEST = 'REQUEST'
    OVERLOAD = 'OVERLOAD'
    MEMORY_PRESSURE = 'MEMORY_PRESSURE'
    NVML_UNAVAILABLE = 'NVML_UNAVAILABLE'
    LOG_UNAVAILABLE = 'LOG_UNAVAILABLE'
    METRICS_UNAVAILABLE = 'METRICS_UNAVAILABLE'
    MANAGER_UNAVAILABLE = 'MANAGER_UNAVAILABLE'
    GRAMMAR_UNAVAILABLE = 'GRAMMAR_UNAVAILABLE'
    MEDIA_UNAVAILABLE = 'MEDIA_UNAVAILABLE'
    CACHE_INVALID = 'CACHE_INVALID'
    MODEL_EXIT = 'MODEL_EXIT'
    UNKNOWN_MODEL_EXIT = 'UNKNOWN_MODEL_EXIT'
    CUDA_CONTEXT = 'CUDA_CONTEXT'
    PLE_UNAVAILABLE = 'PLE_UNAVAILABLE'
    RESOURCE_EXHAUSTED = 'RESOURCE_EXHAUSTED'
    RESOURCE_UNKNOWN = 'RESOURCE_UNKNOWN'
    DEVICE_FAILURE = 'DEVICE_FAILURE'
    IDENTITY_INVALID = 'IDENTITY_INVALID'
    CLEANUP_FAILED = 'CLEANUP_FAILED'
    CONTROLLER_EXIT = 'CONTROLLER_EXIT'
    GATEWAY_EXIT = 'GATEWAY_EXIT'
    LEDGER_UNAVAILABLE = 'LEDGER_UNAVAILABLE'
    UNCONFIRMED_STALL = 'UNCONFIRMED_STALL'
    EXECUTION_DEADLINE = 'EXECUTION_DEADLINE'


class Action(StrEnum):
    IGNORE = 'IGNORE'
    REQUEST_ONLY = 'REQUEST_ONLY'
    KEEP_SERVING = 'KEEP_SERVING'
    CLOSE_ADMISSION = 'CLOSE_ADMISSION'
    DISABLE_CAPABILITY = 'DISABLE_CAPABILITY'
    FREEZE_RECOVERY = 'FREEZE_RECOVERY'
    WAIT_DEPENDENCY = 'WAIT_DEPENDENCY'
    RESTART_MODEL = 'RESTART_MODEL'
    RESTART_CONTROLLER = 'RESTART_CONTROLLER'
    RESTART_GATEWAY = 'RESTART_GATEWAY'
    QUARANTINE = 'QUARANTINE'
    STOP = 'STOP'


@dataclass(frozen=True)
class Evidence:
    cleanup_complete: bool = False
    resources_safe: bool = False
    identity_valid: bool = False
    lease_continuous: bool = False
    device_usable: bool = False
    dependency_ready: bool = False

    def __post_init__(self):
        if any(type(value) is not bool for value in vars(self).values()):
            raise ValueError('safety evidence must be boolean')


@dataclass(frozen=True)
class Fault:
    kind: FaultKind
    controller_generation: int
    engine_generation: int

    def __post_init__(self):
        if not isinstance(self.kind, FaultKind):
            raise ValueError('typed fault required')
        validate_generations(self.controller_generation, self.engine_generation)


@dataclass(frozen=True)
class Context:
    desired: Desired
    controller_generation: int
    engine_generation: int
    evidence: Evidence = Evidence()

    def __post_init__(self):
        if not isinstance(self.desired, Desired) or not isinstance(
                self.evidence, Evidence):
            raise ValueError('typed desired state and evidence required')
        validate_generations(self.controller_generation, self.engine_generation)


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str


AUXILIARY = frozenset((FaultKind.NVML_UNAVAILABLE, FaultKind.LOG_UNAVAILABLE,
                       FaultKind.METRICS_UNAVAILABLE,
                       FaultKind.MANAGER_UNAVAILABLE))
FATAL = frozenset((FaultKind.DEVICE_FAILURE, FaultKind.IDENTITY_INVALID,
                   FaultKind.CLEANUP_FAILED))
MODEL_RECOVERABLE = frozenset((FaultKind.MODEL_EXIT,
    FaultKind.UNKNOWN_MODEL_EXIT, FaultKind.CUDA_CONTEXT,
    FaultKind.PLE_UNAVAILABLE, FaultKind.RESOURCE_EXHAUSTED,
    FaultKind.EXECUTION_DEADLINE))


def validate_generations(*values):
    if any(type(value) is not int or not 0 <= value < (1 << 63)
           for value in values):
        raise ValueError('invalid generation')


def decide(fault: Fault, context: Context) -> Decision:
    """Treat stale events as historical; never lower a live protective fact."""
    if not isinstance(fault.kind, FaultKind) or not isinstance(
            context.desired, Desired):
        raise ValueError('typed fault and desired state required')
    if context.desired != Desired.RUNNING:
        return Decision(Action.STOP, 'operator_intent')
    if (fault.controller_generation != context.controller_generation or
            fault.engine_generation != context.engine_generation):
        return Decision(Action.IGNORE, 'stale_generation')
    kind = fault.kind
    if kind in FATAL:
        return Decision(Action.QUARANTINE, kind.value.lower())
    if kind in AUXILIARY:
        return Decision(Action.KEEP_SERVING, 'auxiliary_degraded')
    if kind in (FaultKind.REQUEST, FaultKind.CACHE_INVALID):
        return Decision(Action.REQUEST_ONLY, 'request_local')
    if kind in (FaultKind.GRAMMAR_UNAVAILABLE, FaultKind.MEDIA_UNAVAILABLE):
        return Decision(Action.DISABLE_CAPABILITY, kind.value.lower())
    if kind in (FaultKind.OVERLOAD, FaultKind.MEMORY_PRESSURE,
                FaultKind.RESOURCE_UNKNOWN):
        return Decision(Action.CLOSE_ADMISSION, kind.value.lower())
    if kind == FaultKind.LEDGER_UNAVAILABLE:
        return Decision(Action.FREEZE_RECOVERY, 'persistence_unavailable')
    if kind == FaultKind.UNCONFIRMED_STALL:
        return Decision(Action.KEEP_SERVING, 'no_confirmed_execution_failure')
    evidence = context.evidence
    # Only identity/cleanup are needed to restart the CPU gateway. It must
    # separately validate backend generation before opening admission.
    if kind == FaultKind.GATEWAY_EXIT:
        if evidence.cleanup_complete and evidence.identity_valid:
            return Decision(Action.RESTART_GATEWAY, 'gateway_exited')
        return Decision(Action.WAIT_DEPENDENCY, 'gateway_cleanup_or_identity')
    if not (evidence.cleanup_complete and evidence.resources_safe and
            evidence.identity_valid and evidence.lease_continuous):
        return Decision(Action.WAIT_DEPENDENCY, 'replacement_preflight')
    if kind == FaultKind.CONTROLLER_EXIT:
        # The CPU controller must be able to inspect/probe an unavailable
        # device. Its replacement does not authorize loading a model.
        return Decision(Action.RESTART_CONTROLLER, 'controller_exited')
    if kind in MODEL_RECOVERABLE:
        if not evidence.device_usable:
            return Decision(Action.WAIT_DEPENDENCY, 'device_not_proven_usable')
        if kind in (FaultKind.PLE_UNAVAILABLE, FaultKind.RESOURCE_EXHAUSTED):
            if not evidence.dependency_ready:
                return Decision(Action.WAIT_DEPENDENCY, 'dependency_not_ready')
        return Decision(Action.RESTART_MODEL, kind.value.lower())
    raise ValueError('unhandled fault')


def decisions(faults, context):
    """Return all effects; a timeout must not mask an explicit device failure.

    Consumers apply STOP/QUARANTINE before any other effects. Keeping effects
    separate also preserves capability loss during an auxiliary observation gap.
    """
    faults = tuple(faults)
    if not faults or len(faults) > len(FaultKind):
        raise ValueError('bounded nonempty fault set required')
    result = tuple(decide(fault, context) for fault in faults)
    for action in (Action.STOP, Action.QUARANTINE):
        if any(item.action == action for item in result):
            return tuple(item for item in result if item.action == action)
    return result
