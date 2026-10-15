"""Successful-path GPU residency replay from captured physical-slot state.

This follows MoEResidency::PlanResolve and successful CommitExpert metadata
updates. Each call commits its entire plan before the next call begins. The
runtime's worker barrier makes GPU endpoints independent of commit order for
the distinct slots/experts in a valid plan. No lower-cache, payload, numerical,
I/O or timing behavior is simulated.

Errors are not transactions: runtime planning may already advance its clock
or touch resident/planned slots before failure. This module likewise does not
promise rollback. Its finite-input contract rejects uint64 clock overflow.
"""

from collections.abc import Iterable, Mapping, Set


UINT64_MAX = (1 << 64) - 1
MAX_NEEDED = 1 << 20
COUNTER_KEYS = (
    "resolve_calls", "expert_lookups", "hits", "misses", "loads",
    "prefill_lookups", "decode_lookups", "prefill_misses", "decode_misses",
    "evictions", "resident_delta", "resident_hits", "planned_hits",
)
SNAPSHOT_KEYS = (
    "slot_experts", "slot_ticks", "slot_protected", "slot_clock",
)


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid {label}")
    return value


class GpuCacheState:
    """One layer, initialized once and carried across calls and requests.

    Extra snapshot fields, such as layer or L2 metadata, are ignored. Required
    GPU fields are copied, so neither input nor returned snapshots alias the
    internal state. `policy='expert_id'` changes only occupied oldest-tick
    ties; empty-slot selection remains in physical-slot order.
    """

    def __init__(self, snapshot, experts=512, policy="physical_slot"):
        self.experts = _integer(experts, 1, MAX_NEEDED, "expert count")
        if policy not in ("physical_slot", "expert_id"):
            raise ValueError("unknown GPU victim policy")
        self.policy = policy
        if (not isinstance(snapshot, Mapping)
                or any(key not in snapshot for key in SNAPSHOT_KEYS)):
            raise ValueError("incomplete GPU snapshot")
        for key in SNAPSHOT_KEYS[:3]:
            if not isinstance(snapshot[key], (list, tuple)):
                raise ValueError(f"invalid {key} array")
        self._slots = list(snapshot["slot_experts"])
        self.capacity = _integer(len(self._slots), 1, self.experts,
                                 "slot capacity")
        self._ticks = list(snapshot["slot_ticks"])
        self._protected = list(snapshot["slot_protected"])
        if (len(self._ticks) != self.capacity
                or len(self._protected) != self.capacity):
            raise ValueError("GPU snapshot array lengths differ")
        self._clock = _integer(snapshot["slot_clock"], 0, UINT64_MAX,
                               "slot clock")
        self._positions = {}
        for slot, expert in enumerate(self._slots):
            _integer(expert, -1, self.experts - 1, "slot expert")
            _integer(self._ticks[slot], 0, self._clock, "slot tick")
            _integer(self._protected[slot], 0, 1, "protection flag")
            if expert >= 0:
                if expert in self._positions:
                    raise ValueError("duplicate occupied expert")
                self._positions[expert] = slot

    def snapshot(self):
        """Return exact physical slot identities, protection and raw clocks."""
        return {
            "slot_experts": list(self._slots),
            "slot_ticks": list(self._ticks),
            "slot_protected": list(self._protected),
            "slot_clock": self._clock,
        }

    def resolve(self, needed, decode_phase=False):
        """Plan ordered lookups, then commit all misses; return call counters.

        `decode_phase` describes the runtime subchunk shape (one token), not
        necessarily the enclosing HTTP phase. A prefill singleton therefore
        uses True while still contributing to the caller's prefill interval.
        """
        if type(decode_phase) is not bool:
            raise ValueError("decode_phase must be bool")
        if (not isinstance(needed, Iterable)
                or isinstance(needed, (Mapping, Set, str, bytes))):
            raise ValueError("needed must be an ordered iterable")
        entries = list(needed)
        counts = dict.fromkeys(COUNTER_KEYS, 0)
        if not entries:
            return counts
        if len(entries) > MAX_NEEDED:
            raise ValueError("resolve set exceeds 1<<20 entries")
        if self._clock == UINT64_MAX:
            raise ValueError("uint64 slot clock overflow is unsupported")
        self._clock += 1
        counts["resolve_calls"] = 1
        for expert in entries:
            _integer(expert, 0, self.experts - 1, "needed expert")
        needed_set = set(entries)
        planned, reserved, plan = {}, set(), []
        phase = "decode" if decode_phase else "prefill"
        for expert in entries:
            counts["expert_lookups"] += 1
            counts[phase + "_lookups"] += 1
            slot = self._positions.get(expert, -1)
            resident = slot >= 0
            if slot < 0:
                slot = planned.get(expert, -1)
            if slot >= 0:
                self._ticks[slot] = self._clock
                counts["hits"] += 1
                counts["resident_hits" if resident else "planned_hits"] += 1
                continue
            counts["misses"] += 1
            counts[phase + "_misses"] += 1
            victim = -1
            for candidate, old in enumerate(self._slots):
                if (old < 0 and not self._protected[candidate]
                        and candidate not in reserved):
                    victim = candidate
                    break
            if victim < 0:
                best_tick = UINT64_MAX
                for candidate, old in enumerate(self._slots):
                    if (self._protected[candidate] or candidate in reserved
                            or old in needed_set):
                        continue
                    tick = self._ticks[candidate]
                    if tick < best_tick:
                        best_tick, victim = tick, candidate
                    elif (self.policy == "expert_id" and victim >= 0
                          and tick == best_tick
                          and old < self._slots[victim]):
                        victim = candidate
            if victim < 0:
                raise ValueError("no residency slot available")
            reserved.add(victim)
            planned[expert] = victim
            plan.append((expert, victim))

        # All slots and experts in this plan are distinct. Commit order cannot
        # alter the GPU endpoint; lower-cache order remains outside this model.
        for expert, slot in plan:
            old = self._slots[slot]
            if old >= 0:
                counts["evictions"] += 1
                del self._positions[old]
            else:
                counts["resident_delta"] += 1
            self._slots[slot] = expert
            self._positions[expert] = slot
            self._ticks[slot] = self._clock
            counts["loads"] += 1
        return counts


def _required_set(values, label, allow_empty_slots=False):
    result = set()
    for expert in values:
        if allow_empty_slots and type(expert) is int and expert == -1:
            continue
        if type(expert) is not int or expert < 0:
            raise ValueError(f"invalid {label} expert")
        result.add(expert)
    return result


def transition_load_lower_bound(previous_needed, next_needed, capacity):
    """Minimum forced loads knowing only two consecutive required sets.

    At most C-|previous| of next-minus-previous can occupy spare slots.
    This bound ignores protection and other history, so it need not be
    achievable jointly with bounds from other transitions.
    """
    _integer(capacity, 1, MAX_NEEDED, "capacity")
    previous = _required_set(previous_needed, "previous")
    following = _required_set(next_needed, "next")
    if max(len(previous), len(following)) > capacity:
        raise ValueError("required set exceeds capacity")
    return max(0, len(following - previous) - (capacity - len(previous)))


def phase_lower_bound(entry_experts, chunks, capacity):
    """Sum entry-missing first demand and adjacent capacity lower bounds.

    `chunks` contains ordered required-ID iterables, not arbitrary trace rows.
    Empty physical entry slots (-1) are ignored. Actual loads minus this bound
    is only an upper bound on avoidable work for the fixed partitions.
    """
    _integer(capacity, 1, MAX_NEEDED, "capacity")
    entry = _required_set(entry_experts, "entry", allow_empty_slots=True)
    if len(entry) > capacity:
        raise ValueError("entry occupancy exceeds capacity")
    total, previous = 0, None
    for chunk in chunks:
        required = _required_set(chunk, "chunk")
        if len(required) > capacity:
            raise ValueError("required set exceeds capacity")
        if previous is None:
            total += len(required - entry)
        else:
            total += transition_load_lower_bound(previous, required, capacity)
        previous = required
    return total
