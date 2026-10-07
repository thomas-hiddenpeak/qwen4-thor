"""Finite mirror opportunity accounting, not a lower-cache replay.

The caller verifies actual GPU planning, slots/ticks/endpoints and capture
identity. Here each successful plan supplies ordered needed/missing IDs and
its distinct occupied GPU victims. These functions validate those local
contracts, exhaust the complete plan input and never change input objects.

Possible mirror support is the observed decode-entry ring union ALL strictly
earlier GPU victims. Skipped writebacks may retain arbitrarily old entries, so
the union is never trimmed to the last K victims. Support, next use and the
intervals below neither establish actual mirror residence nor predict READ
savings, physical I/O, waiting time or speed. Worker ordering is not inferred.
"""

from collections import Counter
from collections.abc import Iterable, Mapping, Set


UINT64_MAX = (1 << 64) - 1
CATEGORIES = ("gpu_only", "l2_only", "both", "sole")


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid {label}")
    return value


def _ordered(values, label):
    if (not isinstance(values, Iterable)
            or isinstance(values, (Mapping, Set, str, bytes, bytearray))):
        raise ValueError(f"{label} must be an ordered iterable")
    return list(values)


def _ids(values, experts, label, empty=False):
    result = _ordered(values, label)
    occupied = set()
    for expert in result:
        _integer(expert, -1 if empty else 0, experts - 1, label + " ID")
        if expert >= 0:
            if expert in occupied:
                raise ValueError(f"duplicate {label} expert")
            occupied.add(expert)
    return result


def _dimensions(experts, gpu_slots, l2_slots, mirror_k):
    _integer(experts, 1, 1 << 20, "expert count")
    _integer(gpu_slots, 1, experts, "GPU slots")
    _integer(l2_slots, 0, experts, "L2 slots")
    _integer(mirror_k, 0, experts, "mirror slots")


def classify_snapshot(layer, *, experts=512, gpu_slots=256,
                      l2_slots=16, mirror_k=8):
    """Partition occupied mirror IDs by GPU/L2 coverage at ONE snapshot.

    Lists preserve physical mirror-slot order. ``empty_slots`` contains slot
    indices, never expert IDs. Repeated -1 empties are valid; occupied IDs in
    each cache must be distinct. Cross-cache duplicates are intentionally
    allowed. Only membership arrays and cursor are required here; the caller
    validates the full raw GPU/L2 snapshot and enclosing phase identity.
    """
    _dimensions(experts, gpu_slots, l2_slots, mirror_k)
    keys = ("slot_experts", "l2_experts", "mirror_experts", "mirror_cursor")
    if not isinstance(layer, Mapping) or any(key not in layer for key in keys):
        raise ValueError("incomplete cache membership snapshot")
    arrays = []
    for key, length in zip(keys[:3], (gpu_slots, l2_slots, mirror_k)):
        if not isinstance(layer[key], (list, tuple)):
            raise ValueError(f"invalid {key} snapshot array")
        ids = _ids(layer[key], experts, key, empty=True)
        if len(ids) != length:
            raise ValueError(f"wrong {key} snapshot length")
        arrays.append(ids)
    cursor = _integer(layer["mirror_cursor"], 0, max(0, mirror_k - 1),
                      "mirror cursor")
    gpu, l2, ring = set(arrays[0]), set(arrays[1]), arrays[2]
    result = {key: [] for key in (*CATEGORIES, "empty_slots")}
    for slot, expert in enumerate(ring):
        if expert < 0:
            result["empty_slots"].append(slot)
        else:
            in_gpu, in_l2 = expert in gpu, expert in l2
            category = ("both" if in_gpu and in_l2 else
                        "gpu_only" if in_gpu else
                        "l2_only" if in_l2 else "sole")
            result[category].append(expert)
    result["mirror_cursor"] = cursor
    result["counts"] = {key: len(result[key])
                        for key in (*CATEGORIES, "empty_slots")}
    return result


def _plans(plans, experts, top_k, expected_plans):
    _integer(top_k, 1, experts, "top-k")
    _integer(expected_plans, 0, 1 << 20, "expected plan count")
    rows = _ordered(plans, "plans")  # Exhaust even a generator/bad trailer.
    if len(rows) != expected_plans:
        raise ValueError("incomplete or excess plan sequence")
    result = []
    for step, row in enumerate(rows, 1):
        if not isinstance(row, Mapping) or any(
                key not in row for key in ("needed", "missing", "victims")):
            raise ValueError(f"incomplete plan {step}")
        needed = _ids(row["needed"], experts, "needed")
        missing = _ids(row["missing"], experts, "missing")
        victims = _ids(row["victims"], experts, "victim")
        if len(needed) != top_k:
            raise ValueError(f"wrong needed count in plan {step}")
        needed_set, missing_set = set(needed), set(missing)
        if not missing_set <= needed_set:
            raise ValueError("GPU missing expert is not needed")
        if missing != [expert for expert in needed if expert in missing_set]:
            raise ValueError("missing order differs from needed order")
        if set(victims) & needed_set:
            raise ValueError("GPU victim intersects complete needed set")
        if len(victims) > len(missing):
            raise ValueError("more occupied victims than GPU misses")
        result.append(dict(needed=needed, missing=missing, victims=victims))
    return result


def _histogram(values):
    return {str(key): values[key] for key in sorted(values)}


def _analyze(entry_layer, classification, plans, observed_candidates,
             mirror_k, include_plan_support):
    _integer(observed_candidates, 0, UINT64_MAX, "observed candidate count")
    entry = {expert for expert in entry_layer["mirror_experts"] if expert >= 0}
    possible = set(entry)
    latest = {}  # expert -> (victim plan, cumulative victims through that plan)
    victim_prefix = 0
    plan_gaps, between_counts = Counter(), Counter()
    support_counts = dict.fromkeys(("entry_only", "prior_victim_only",
                                   "entry_and_prior_victim", "unsupported"), 0)
    first = {}
    category_of = {expert: category for category in CATEGORIES
                   for expert in classification[category]}
    for slot, expert in enumerate(entry_layer["mirror_experts"]):
        if expert >= 0:
            first[expert] = dict(expert=expert, mirror_slot=slot,
                entry_category=category_of[expert], first_gpu_eviction_plan=None,
                first_gpu_miss_plan=None)
    total, supported_total, capacity = 0, 0, 0
    per_plan, supported_sets = [], []
    for step, plan in enumerate(plans, 1):
        missing, victims = plan["missing"], plan["victims"]
        supported = [expert for expert in missing if expert in possible]
        supported_sets.append(set(supported))
        cap = min(mirror_k, len(supported))
        total += len(missing)
        supported_total += len(supported)
        capacity += cap
        recent = []
        for expert in missing:
            in_entry, in_prior = expert in entry, expert in latest
            category = ("entry_and_prior_victim" if in_entry and in_prior else
                        "entry_only" if in_entry else
                        "prior_victim_only" if in_prior else "unsupported")
            support_counts[category] += 1
            if expert in first and first[expert]["first_gpu_miss_plan"] is None:
                first[expert]["first_gpu_miss_plan"] = step
            if in_prior:
                prior_step, prefix_through_prior = latest[expert]
                gap = step - prior_step
                between = victim_prefix - prefix_through_prior
                plan_gaps[gap] += 1
                between_counts[between] += 1
                recent.append(dict(expert=expert, victim_plan=prior_step,
                    plan_gap=gap, strictly_intervening_victims=between))
        if include_plan_support:
            per_plan.append(dict(plan=step,
                possible_mirror_ids=sorted(possible),
                entry_mirror_supported_missing=[e for e in missing if e in entry],
                prior_victim_supported_missing=[e for e in missing if e in latest],
                possible_supported_missing=list(supported),
                unsupported_missing=[e for e in missing if e not in possible],
                candidate_capacity=cap, latest_prior_victim=recent))
        # No current-plan victim is an entry source. Exclude the complete
        # source plan and current plan from "strictly intervening" counts;
        # ordering among victims in either plan is unknown.
        victim_prefix += len(victims)
        for expert in victims:
            if expert in first and first[expert]["first_gpu_eviction_plan"] is None:
                first[expert]["first_gpu_eviction_plan"] = step
            latest[expert] = (step, victim_prefix)
        possible.update(victims)
    if observed_candidates > capacity:
        raise ValueError("observed candidates exceed potential support capacity")
    for row in first.values():
        row["eviction_censored"] = row["first_gpu_eviction_plan"] is None
        row["miss_censored"] = row["first_gpu_miss_plan"] is None
        row["censor_after_plan"] = len(plans)
    result = dict(plan_count=len(plans), missing_total=total,
        supported_missing_total=supported_total,
        potential_candidate_capacity=capacity,
        observed_candidates=observed_candidates,
        support_counts=support_counts,
        latest_prior_victim_plan_gap_histogram=_histogram(plan_gaps),
        strictly_intervening_victims_histogram=_histogram(between_counts),
        entry_mirror_first_events=list(first.values()),
        entry_classification=classification)
    if include_plan_support:
        result["plan_support"] = per_plan
    return result, supported_sets


def analyze_layer(entry_layer, plans, observed_candidates, *, mirror_k=8,
                  experts=512, gpu_slots=256, l2_slots=16, top_k=10,
                  expected_plans=255, include_plan_support=False):
    """Analyze one full captured decode phase of one layer.

    ``plans`` has implicit one-based positions and must contain exactly
    ``expected_plans`` records. Plan gap is i-j for the latest prior victim
    plan j. Intervening victims count all occupied victims in j<plan<i;
    same-plan peer order is never guessed. Entry-only support has no invented
    eviction date and contributes to neither distance histogram.

    This checks internal plan facts, not physical GPU slots/LRU/capacity or
    snapshot clocks. The runner must validate those using the frozen core.
    Smaller explicit dimensions are for finite host contracts only.
    """
    if type(include_plan_support) is not bool:
        raise ValueError("include_plan_support must be bool")
    classification = classify_snapshot(entry_layer, experts=experts,
        gpu_slots=gpu_slots, l2_slots=l2_slots, mirror_k=mirror_k)
    rows = _plans(plans, experts, top_k, expected_plans)
    return _analyze(entry_layer, classification, rows, observed_candidates,
                    mirror_k, include_plan_support)[0]


def compare_layers(S_entry, S_plans, S_C, L_entry, L_plans, L_C, *,
                   mirror_k=8, experts=512, gpu_slots=256, l2_slots=16,
                   top_k=10, expected_plans=255):
    """Bound S-L entry-candidate difference on aligned common GPU misses.

    Per-plan common and only support counts are independently capped at K;
    total support is capped separately. With observed total C, the aggregate
    common count lies in [max(0,C-U_only), min(C,U_common)]. S-L subtracts
    intervals. This is a conservative feasible superset, not a point causal
    split; independent common/only caps need not be jointly attainable.
    Route mismatch is returned, not discarded or relabelled a controlled
    comparison. Here route_equal means ordered needed IDs, not route weights.
    """
    classifications = [classify_snapshot(layer, experts=experts,
        gpu_slots=gpu_slots, l2_slots=l2_slots, mirror_k=mirror_k)
        for layer in (S_entry, L_entry)]
    rows = [_plans(plans, experts, top_k, expected_plans)
            for plans in (S_plans, L_plans)]
    analyses = [_analyze(entry, classification, plans, observed, mirror_k, False)
                for entry, classification, plans, observed in zip(
                    (S_entry, L_entry), classifications, rows, (S_C, L_C))]
    counts = dict.fromkeys(("common", "S_only", "L_only", "S_total", "L_total"), 0)
    caps = {side: dict(common=0, only=0, total=0) for side in ("S", "L")}
    mismatches, set_mismatches, per_plan = [], [], []
    for step, (short, long) in enumerate(zip(*rows), 1):
        if short["needed"] != long["needed"]:
            mismatches.append(step)
        if set(short["needed"]) != set(long["needed"]):
            set_mismatches.append(step)
        short_miss, long_miss = set(short["missing"]), set(long["missing"])
        common = short_miss & long_miss
        only = (short_miss - long_miss, long_miss - short_miss)
        count = dict(common=len(common), S_only=len(only[0]), L_only=len(only[1]),
                     S_total=len(short_miss), L_total=len(long_miss))
        for key, value in count.items():
            counts[key] += value
        plan_caps = {}
        for index, side in enumerate(("S", "L")):
            support = analyses[index][1][step - 1]
            values = dict(common=min(mirror_k, len(support & common)),
                          only=min(mirror_k, len(support & only[index])),
                          total=min(mirror_k, len(support)))
            plan_caps[side] = values
            for key, value in values.items():
                caps[side][key] += value
        per_plan.append(dict(plan=step, miss_counts=count, capacities=plan_caps))
    intervals = {side: dict(lower=max(0, observed - caps[side]["only"]),
                           upper=min(observed, caps[side]["common"]))
                 for side, observed in (("S", S_C), ("L", L_C))}
    return dict(plan_count=expected_plans,
        route_equal=not mismatches, route_mismatch_plans=mismatches,
        needed_sets_equal=not set_mismatches,
        needed_set_mismatch_plans=set_mismatches,
        miss_counts=counts, capacities=caps,
        observed_candidates=dict(S=S_C, L=L_C),
        common_candidate_intervals=intervals,
        common_candidate_difference_interval=dict(
            lower=intervals["S"]["lower"] - intervals["L"]["upper"],
            upper=intervals["S"]["upper"] - intervals["L"]["lower"]),
        per_plan=per_plan)
