"""Fresh RAM evidence for qualified profiles. Unknown never means pressure."""
import math

from tools.serve_recovery import ledger, profile_contract, profile_policy

MIB = 1024**2
MAX_GAP_NS = 2*ledger.SECOND


def available(value):
    ledger.require(type(value) is dict, 'missing memory observation')
    total, free = value.get('total_bytes'), value.get('available_bytes')
    ledger.require(ledger.integer(total, minimum=1) and ledger.integer(free) and
                   free <= total, 'invalid memory observation')
    return free


def cold(registry, state, observation):
    """Propose only; neither this nor a profile receipt permits allocation."""
    profile_contract.validate(registry)
    profile_policy.validate(state)
    ledger.require(state['registry_sha256'] == profile_contract.digest(registry),
                   'profile qualification changed')
    selected = profile_policy.selected_for_launch(state)
    try:
        free = available(observation)
    except (ValueError, KeyError, TypeError):
        return dict(ready=False, target=None, reason='memory_unknown')
    needed = {name: q['startup_required_bytes']
              for name, q in registry['qualifications'].items()}
    if free >= needed[selected]:
        return dict(ready=True, target=selected, reason='qualified_capacity')
    if (selected == 'primary' and state['pending'] is None and
            free >= needed['light']):
        return dict(ready=False, target='light', reason='cold_memory_capacity')
    return dict(ready=False, target=None, reason='memory_headroom')


class Detector:
    """Require continuous, new, reliable samples; gaps reset hysteresis."""
    def __init__(self, registry, config):
        profile_contract.validate(registry)
        self.config = profile_policy.configuration(config)
        q = registry['qualifications']
        extra = q['primary']['startup_required_bytes'] - q['light']['startup_required_bytes']
        self.low = self.config['degrade_below_mib']*MIB
        self.high = self.config['recover_above_mib']*MIB + extra
        self.reset()

    def reset(self):
        self.last = self.since = self.direction = None

    def assess(self, selected, snapshot, now_ns, *, ready=True):
        ledger.require(selected in profile_contract.NAMES and ledger.integer(now_ns),
                       'invalid profile observation context')
        try:
            ledger.require(ready and snapshot['state'] in ('OPEN', 'LOW'),
                           'memory not observed')
            free = available(snapshot)
            at = snapshot['sampled_monotonic']
            ledger.require(type(at) in (float, int) and math.isfinite(at) and at >= 0,
                           'invalid memory observation time')
            sampled = int(at*ledger.SECOND)
            ledger.require(0 <= now_ns-sampled <= MAX_GAP_NS, 'stale memory observation')
        except (ValueError, KeyError, TypeError, OverflowError):
            self.reset()
            return None
        if self.last is not None:
            if sampled < self.last or sampled-self.last > MAX_GAP_NS:
                self.reset()
            elif sampled == self.last:
                return None  # Polling one cached sample cannot earn time.
        self.last = sampled
        direction = ('light' if selected == 'primary' and free < self.low else
                     'primary' if selected == 'light' and free >= self.high else None)
        if direction is None:
            self.since = self.direction = None
            return None
        if self.direction != direction:
            self.direction, self.since = direction, sampled
        duration = self.config['pressure_seconds' if direction == 'light' else 'recovery_seconds']
        if sampled-self.since < duration*ledger.SECOND:
            return None
        return dict(target=direction, reason=('sustained_memory_pressure'
            if direction == 'light' else 'sustained_memory_recovery'))
