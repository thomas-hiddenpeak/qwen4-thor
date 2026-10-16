"""Host-only replay of Phase-D-off MoEResidency; no trace reader or CLI.

GPU identities, plan ticks and fixed partitions follow the runtime given the
actual C++ token_order. Router traces do NOT determine concurrent worker order
or CUDA event readiness. Lower-cache results therefore have an explicit
schedule, not a claim of exact hardware reproduction or a performance bound:

* serial_eager: dispatch tasks execute serially and events are ready at queries.
* batch_deferred: all tasks claim before any commits; events only complete at
  actual blocking waits or the next forward's router-copy synchronization.

Both retain StageTask's within-task claim/commit order and merge grouping. They
are examples of legal schedules, NOT extrema of all schedules. Initial loading
also populates L2; GPU/L2/mirror can hold duplicate experts. No Linux pagecache,
physical NVMe traffic, bandwidth, overlap time, or TTFT is predicted here.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Collection, Iterable, Mapping, Sequence


COUNTERS = (
    "gpu_lookups", "gpu_hits", "gpu_misses", "gpu_resident_route_hits",
    "gpu_planned_route_hits", "gpu_loads", "gpu_evictions", "l2_hits", "l2_misses",
    "l2_evictions", "mirror_hits", "nvme_reads", "mirror_writes",
    "mirror_evictions", "mirror_duplicate_invalidations", "mirror_skips",
    "subchunks", "stage_batches", "dispatch_tasks",
)


def _counts() -> Counter:
    return Counter(dict.fromkeys(COUNTERS, 0))


@dataclass(frozen=True)
class ReplayConfig:
    experts: int = 512
    capacity: int = 256
    l2_slots: int = 16
    mirror_slots: int = 8
    load_threads: int = 16
    pread_merge: bool = True
    pread_merge_cap: int = 4
    inline_miss_limit: int = 1
    schedule: str = "serial_eager"
    hot_protect: bool = False
    require_token_order: bool = True
    contiguous_next_by_layer: Mapping[int, Collection[int]] = field(
        default_factory=dict)

    def __post_init__(self):
        if not (0 < self.capacity <= self.experts):
            raise ValueError("capacity must be in [1, experts]")
        if not (0 < self.load_threads <= 16):
            raise ValueError("invalid effective load_threads")
        if self.l2_slots < self.load_threads or self.l2_slots > 512:
            raise ValueError("use effective L2 slots (>= load_threads, <=512)")
        if not (0 <= self.mirror_slots <= 32):
            raise ValueError("invalid effective mirror_slots")
        if not (1 <= self.pread_merge_cap <= 8):
            raise ValueError("invalid effective pread_merge_cap")
        if not (0 <= self.inline_miss_limit <= 2):
            raise ValueError("use effective inline_miss_limit in [0, 2]")
        if self.schedule not in ("serial_eager", "batch_deferred"):
            raise ValueError("unknown assumed worker/event schedule")
        if self.hot_protect:
            raise ValueError("hot_protect unsupported: runtime partition counts "
                             "protected IDs despite its comment")


@dataclass(frozen=True)
class ForwardTrace:
    layer: int
    forward_id: str
    request_id: str
    phase: str
    topk_ids: Sequence[int]  # Flat, e.g. array('H'); router top-k order retained.
    topk: int = 10
    token_order: Sequence[int] | None = None


@dataclass(frozen=True)
class Chunk:
    index: int
    token_indices: tuple[int, ...]
    experts: tuple[int, ...]  # Distinct IDs in first occurrence order.
    lookups: int
    frequencies: Mapping[int, int]


@dataclass(frozen=True)
class ReplayEvent:
    event_index: int
    layer: int
    expert: int
    kind: str
    request_id: str
    forward_id: str
    trace_phase: str
    runtime_phase: str
    chunk_index: int
    execution_index: int
    batch_index: int


@dataclass
class ForwardResult:
    layer: int
    forward_id: str
    request_id: str
    phase: str
    counts: dict
    by_runtime_phase: dict
    chunk_order: list[int]
    chunk_sizes: list[int]
    chunk_distinct: list[int]
    fixed_partition_oracle_gpu_misses: int
    resident_before: tuple[int, ...]
    resident_after: tuple[int, ...]
    occupancy: dict


@dataclass
class ReplayResult:
    aggregate: dict
    by_trace_phase: dict
    by_runtime_phase: dict
    forwards: list[ForwardResult]
    final_states: dict
    assumptions: dict
    events: list[ReplayEvent]


def partition_forward(trace: ForwardTrace, config: ReplayConfig) -> list[Chunk]:
    """moe.cu partition, including re-probe after a full chunk flush.

    Python's stable sort cannot substitute for libstdc++ std::sort when equal
    sorted-top-k keys have different router order. Exact mode requires the
    caller's C++ adapter order; explicitly approximate mode is available only
    for small host fixtures/exploration.
    """
    ids, k = trace.topk_ids, trace.topk
    if k <= 0 or len(ids) == 0 or len(ids) % k:
        raise ValueError("nonempty flat topk_ids must be divisible by topk")
    if config.capacity < k:
        raise ValueError("runtime rejects capacity smaller than topk")
    if trace.phase not in ("prefill", "decode"):
        raise ValueError("phase must be prefill or decode")
    if any(e < 0 or e >= config.experts for e in ids):
        raise ValueError("expert ID out of range")
    rows = len(ids) // k
    keys = [tuple(sorted(ids[t * k:(t + 1) * k])) for t in range(rows)]
    if trace.token_order is None:
        if rows > 1 and config.require_token_order:
            raise ValueError("actual C++ token_order required for exact replay")
        order = sorted(range(rows), key=keys.__getitem__)
    else:
        order = list(trace.token_order)
        if sorted(order) != list(range(rows)):
            raise ValueError("token_order must be a row permutation")
        if any(keys[a] > keys[b] for a, b in zip(order, order[1:])):
            raise ValueError("token_order does not follow sorted top-k keys")

    chunks, tokens, needed = [], [], set()

    def flush():
        if len(tokens) * k > (1 << 20):
            raise ValueError("runtime PlanResolve set exceeds 1<<20 entries")
        frequencies = Counter(e for t in tokens for e in ids[t*k:(t+1)*k])
        chunks.append(Chunk(len(chunks), tuple(tokens), tuple(frequencies),
                            len(tokens) * k, dict(frequencies)))

    for token in order:
        row = set(ids[token * k:(token + 1) * k])
        if len(needed | row) > config.capacity:
            if tokens:
                flush()
            tokens, needed = [], set()
        needed.update(row)
        tokens.append(token)
    if tokens:
        flush()
    return chunks


def fixed_partition_oracle(chunks: Sequence[Chunk], capacity: int,
                           initial_resident: Collection[int]) -> int:
    """Minimum GPU loads for these mandatory sets, with this entry state.

    Farthest-next-use eviction is offline Belady on set requests: a complete
    chunk must fit simultaneously. This is only a same-entry, one-forward
    fixed-order miss lower bound (hit upper bound). It says nothing about the
    next forward, L2/mirror, bytes, execution time, or the greedy chunk order.
    """
    resident = set(initial_resident)
    if len(resident) > capacity:
        raise ValueError("oracle entry state exceeds capacity")
    future = defaultdict(list)
    for i, chunk in enumerate(chunks):
        if len(chunk.experts) > capacity:
            raise ValueError("oracle chunk exceeds capacity")
        for expert in chunk.experts:
            future[expert].append(i)
    cursors = defaultdict(int)
    misses = 0
    for i, chunk in enumerate(chunks):
        needed = set(chunk.experts)
        for expert in needed:
            cursors[expert] += 1
        incoming = needed - resident
        misses += len(incoming)
        evict_n = max(0, len(resident) + len(incoming) - capacity)

        def next_use(expert):
            pos = cursors[expert]
            uses = future[expert]
            return uses[pos] if pos < len(uses) else len(chunks)

        victims = sorted(resident - needed,
                         key=lambda e: (next_use(e), e), reverse=True)
        resident.difference_update(victims[:evict_n])
        resident.update(incoming)
    return misses


class _Layer:
    def __init__(self, config: ReplayConfig, layer: int, record):
        self.config, self.layer, self.record = config, layer, record
        if config.pread_merge and layer not in config.contiguous_next_by_layer:
            raise ValueError(f"layer {layer}: actual contig_next metadata required")
        self.contiguous = set(config.contiguous_next_by_layer.get(layer, ()))
        if any(e < 0 or e + 1 >= config.experts for e in self.contiguous):
            raise ValueError("contig_next IDs out of range")
        self.gpu = [-1] * config.capacity
        self.gpu_pos = {}
        self.gpu_tick = [0] * config.capacity
        self.tick = 0
        self.l2 = [-1] * config.l2_slots
        self.l2_pos = {}
        self.l2_tick = [0] * config.l2_slots
        self.recency = 0
        self.l2_claimed = set()
        self.l2_event = [0] * config.l2_slots
        self.mirror = [-1] * config.mirror_slots
        self.mirror_pos = {}
        self.mirror_claimed = set()
        self.mirror_event = [0] * config.mirror_slots
        self.mirror_cursor = 0
        self.stream_event = self.completed_event = 0

    def _new_event(self):
        self.stream_event += 1
        return self.stream_event

    def synchronize(self):
        # Router ID/weight D2H cudaStreamSynchronize before every forward.
        self.completed_event = self.stream_event

    def _ready(self, event):
        if self.config.schedule == "serial_eager":
            self.completed_event = self.stream_event
        return event <= self.completed_event

    def _claim(self, expert, needed):
        if expert in self.l2_pos:
            b = self.l2_pos[expert]
            self.recency += 1
            self.l2_tick[b] = self.recency
            self.l2_claimed.add(b)
            self.record("l2_hits", "l2_hit", expert)
            return "l2", b
        m = self.mirror_pos.get(expert, -1)
        if m >= 0 and m not in self.mirror_claimed:
            self.mirror_claimed.add(m)
            self.record("mirror_hits", "mirror_hit", expert)
            return "mirror", m
        available = [b for b in range(len(self.l2))
                     if self._ready(self.l2_event[b])
                     and b not in self.l2_claimed]
        preferred = [b for b in available if self.l2[b] not in needed]
        if preferred or available:
            b = min(preferred or available, key=lambda b: (self.l2_tick[b], b))
        else:
            inflight = [b for b in range(len(self.l2))
                        if self.l2_event[b] > self.completed_event]
            if not inflight:
                raise RuntimeError("all L2 slots claimed: invalid schedule/config")
            b = min(inflight, key=lambda b: (self.l2_tick[b], b))
            if b in self.l2_claimed:
                raise RuntimeError("schedule would overwrite a claimed L2 buffer")
            self.completed_event = max(self.completed_event, self.l2_event[b])
        old = self.l2[b]
        if old >= 0:
            del self.l2_pos[old]
            self.record("l2_evictions", "l2_evict", old)
        self.l2[b] = expert
        self.l2_pos[expert] = b
        self.recency += 1
        self.l2_tick[b] = self.recency
        self.l2_claimed.add(b)
        self.record("l2_misses", None, expert)
        self.record("nvme_reads", "nvme_read", expert)
        return "l2", b

    def _commit(self, expert, slot, source):
        kind, b = source
        old = self.gpu[slot]
        if old >= 0:
            self.record("gpu_evictions", "gpu_evict", old)
        if old >= 0 and self.mirror:
            m = -1
            for i in range(len(self.mirror)):
                candidate = (self.mirror_cursor + i) % len(self.mirror)
                if (self._ready(self.mirror_event[candidate])
                        and candidate not in self.mirror_claimed):
                    m = candidate
                    break
            if m < 0:
                self.record("mirror_skips", "mirror_skip", old)
            else:
                self.mirror_cursor = (m + 1) % len(self.mirror)
                prev = self.mirror[m]
                if prev >= 0:
                    self.mirror_pos.pop(prev, None)
                    if prev != old:
                        self.record("mirror_evictions", "mirror_evict", prev)
                for j, mirrored in enumerate(self.mirror):
                    if j != m and mirrored == old:
                        self.mirror[j] = -1
                        self.record("mirror_duplicate_invalidations",
                                    "mirror_invalidate", old)
                self.mirror[m] = old
                self.mirror_pos[old] = m
                self.mirror_event[m] = self._new_event()
                self.record("mirror_writes", "mirror_write", old)
        if kind == "mirror":
            # D2H source may be unfinished. Synchronizing its event completes
            # all earlier events on the same stream, but not the new writeback.
            self.completed_event = max(self.completed_event, self.mirror_event[b])
            self.mirror_event[b] = self._new_event()
            self.mirror_claimed.remove(b)
        else:
            self.l2_event[b] = self._new_event()
            self.l2_claimed.remove(b)
        if old >= 0:
            del self.gpu_pos[old]
        self.gpu[slot] = expert
        self.gpu_pos[expert] = slot
        self.gpu_tick[slot] = self.tick
        self.record("gpu_loads", None, expert)

    def _tasks(self, plan):
        if not self.config.pread_merge:
            return [[entry] for entry in plan]
        ordered = sorted(plan)
        tasks = []
        for entry in ordered:
            expert = entry[0]
            if (tasks and len(tasks[-1]) < self.config.pread_merge_cap
                    and expert == tasks[-1][-1][0] + 1
                    and expert - 1 in self.contiguous):
                tasks[-1].append(entry)
            else:
                tasks.append([entry])
        return tasks

    def _load(self, plan, needed, set_batch):
        for off in range(0, len(plan), self.config.load_threads):
            set_batch(off // self.config.load_threads)
            batch = plan[off:off + self.config.load_threads]
            tasks = self._tasks(batch)
            self.record("stage_batches", None, -1)
            self.record("dispatch_tasks", None, -1, len(tasks))
            # Inline StageTask executes each task to completion even under the
            # deferred schedule, matching LoadPhase1's small-batch branch.
            serial = (self.config.schedule == "serial_eager"
                      or len(batch) <= self.config.inline_miss_limit)
            pending = []
            for task in tasks:
                claimed = [(e, s, self._claim(e, needed)) for e, s in task]
                if serial:
                    for e, s, source in claimed:
                        self._commit(e, s, source)
                else:
                    pending.extend(claimed)
            for e, s, source in pending:
                self._commit(e, s, source)

    def init_hot(self, hot, set_batch):
        # InitHot ignores invalid IDs, caps the list at C, and loads with no
        # needed_mark. Duplicate valid hot IDs expose an existing InitHot bug;
        # reject them rather than silently fixing or concealing the runtime.
        valid = [int(e) for e in hot if 0 <= e < self.config.experts]
        if len(valid) != len(set(valid)):
            raise ValueError("duplicate initial hot experts unsupported")
        valid = valid[:self.config.capacity]
        self._load(list(zip(valid, range(len(valid)))), set(), set_batch)
        for slot in range(len(valid)):
            self.tick += 1
            self.gpu_tick[slot] = self.tick

    def resolve(self, chunk, set_batch):
        self.tick += 1
        needed = set(chunk.experts)
        # All needed and all reserved slots are ineligible. Therefore the
        # runtime's repeated victim scans equal this once-sorted candidate list.
        empty = [s for s, e in enumerate(self.gpu) if e < 0]
        occupied = sorted((s for s, e in enumerate(self.gpu)
                           if e >= 0 and e not in needed),
                          key=lambda s: (self.gpu_tick[s], s))
        victims = iter(empty + occupied)
        plan = []
        resident_hits = 0
        for expert in chunk.experts:
            if expert in self.gpu_pos:
                self.gpu_tick[self.gpu_pos[expert]] = self.tick
                resident_hits += chunk.frequencies[expert]
            else:
                slot = next(victims, None)
                if slot is None:
                    raise RuntimeError("no GPU residency slot available")
                plan.append((expert, slot))
        misses = len(plan)
        self.record("gpu_lookups", None, -1, chunk.lookups)
        self.record("gpu_misses", None, -1, misses)
        self.record("gpu_hits", None, -1, chunk.lookups - misses)
        self.record("gpu_resident_route_hits", None, -1, resident_hits)
        self.record("gpu_planned_route_hits", None, -1,
                    chunk.lookups - misses - resident_hits)
        self.record("subchunks", None, -1)
        self._load(plan, needed, set_batch)

    def snapshot(self):
        gpu, l2, mirror = set(self.gpu_pos), set(self.l2_pos), set(self.mirror_pos)
        return {
            "gpu_slots": tuple(self.gpu), "gpu_ticks": tuple(self.gpu_tick),
            "l2_slots": tuple(self.l2), "l2_ticks": tuple(self.l2_tick),
            "mirror_slots": tuple(self.mirror), "mirror_cursor": self.mirror_cursor,
            "occupancy": {
                "gpu": len(gpu), "l2": len(l2), "mirror": len(mirror),
                "l2_extra_experts": len(l2 - gpu),
                "mirror_extra_experts": len(mirror - gpu),
                "host_extra_experts": len((l2 | mirror) - gpu),
                "total_unique_experts": len(gpu | l2 | mirror),
            },
        }


def replay(forwards: Iterable[ForwardTrace], config: ReplayConfig,
           initial_hot_by_layer: Mapping[int, Sequence[int]], *,
           policy: str = "baseline",
           observer: Callable[[ReplayEvent], None] | None = None,
           retain_events: bool = False) -> ReplayResult:
    """Consume ordered forwards, retaining per-layer cache across requests.

    Initial hot loads are charged separately as phase initial_hot (runtime
    internally uses its default prefill flag). All aggregates include these
    loads; each ForwardResult excludes them. `gpu_hits` follows PlanResolve,
    including repeat lookups satisfied by an as-yet uncommitted planned miss.
    `mirror_evictions` counts replacement of a different valid cached expert;
    duplicate invalidations are separate. No counter represents physical I/O.
    """
    if policy not in ("baseline", "greedy_overlap"):
        raise ValueError("only baseline and the frozen greedy candidate supported")
    aggregate = _counts()
    by_trace, by_runtime = defaultdict(_counts), defaultdict(_counts)
    layers, results, events = {}, [], []
    context = {"layer": -1, "request_id": "", "forward_id": "initial_hot",
               "trace_phase": "initial_hot", "runtime_phase": "initial_hot",
               "chunk_index": -1, "execution_index": -1, "batch_index": -1}
    current = None
    current_runtime = None
    event_index = 0

    def record(counter, kind, expert, amount=1):
        nonlocal event_index
        aggregate[counter] += amount
        by_trace[context["trace_phase"]][counter] += amount
        by_runtime[context["runtime_phase"]][counter] += amount
        if current is not None:
            current[counter] += amount
            current_runtime[context["runtime_phase"]][counter] += amount
        if kind is not None:
            event = ReplayEvent(event_index, expert=expert, kind=kind, **context)
            event_index += 1
            if observer is not None:
                observer(event)
            if retain_events:
                events.append(event)

    def set_batch(batch):
        context["batch_index"] = batch

    # Model loading initializes all configured layers before request inference.
    # Preserve mapping iteration order for the load stream. Missing layer hot
    # metadata is an error, not an invented cold-start assumption.
    for layer_id, hot in initial_hot_by_layer.items():
        context["layer"] = layer_id
        layer = layers[layer_id] = _Layer(config, layer_id, record)
        layer.init_hot(hot, set_batch)

    for trace in forwards:
        if trace.layer not in layers:
            raise ValueError(f"initial hot metadata missing for layer {trace.layer}")
        layer = layers[trace.layer]
        chunks = partition_forward(trace, config)
        current, current_runtime = _counts(), defaultdict(_counts)
        context.update(layer=trace.layer, request_id=trace.request_id,
                       forward_id=trace.forward_id, trace_phase=trace.phase)
        layer.synchronize()
        before = tuple(layer.gpu)
        oracle = fixed_partition_oracle(chunks, config.capacity, layer.gpu_pos)
        remaining = list(chunks)
        execution = []
        while remaining:
            if policy == "greedy_overlap" and trace.phase != "decode":
                chunk = min(remaining, key=lambda c: (
                    -sum(e in layer.gpu_pos for e in c.experts), c.index))
                remaining.remove(chunk)
            else:
                chunk = remaining.pop(0)
            context.update(chunk_index=chunk.index, execution_index=len(execution),
                           runtime_phase=("decode" if len(chunk.token_indices) == 1
                                          else "prefill"), batch_index=-1)
            execution.append(chunk.index)
            layer.resolve(chunk, set_batch)
        if policy == "baseline" and current["gpu_misses"] < oracle:
            raise RuntimeError("baseline GPU misses violate same-entry oracle")
        if (current["gpu_misses"] != current["gpu_loads"]
                or current["gpu_loads"] != current["l2_hits"]
                + current["mirror_hits"] + current["nvme_reads"]):
            raise RuntimeError("GPU load/source accounting invariant failed")
        results.append(ForwardResult(
            trace.layer, trace.forward_id, trace.request_id, trace.phase,
            dict(current), {p: dict(c) for p, c in current_runtime.items()},
            execution, [len(c.token_indices) for c in chunks],
            [len(c.experts) for c in chunks], oracle, before, tuple(layer.gpu),
            layer.snapshot()["occupancy"]))
    return ReplayResult(
        dict(aggregate), {p: dict(c) for p, c in by_trace.items()},
        {p: dict(c) for p, c in by_runtime.items()}, results,
        {i: layer.snapshot() for i, layer in layers.items()},
        {"policy": policy, "lower_cache_schedule": config.schedule,
         "lower_cache_is_actual_execution": False,
         "lower_cache_schedules_are_bounds": False,
         "phase_d": False, "hot_protect": False,
         "exact_token_order_required": config.require_token_order,
         "initial_hot_charged_separately": True,
         "state_carried_across_requests": True,
         "oracle_scope": "fixed partition/order, same entry GPU state, one forward",
         "physical_io_or_latency_prediction": False}, events)
