"""Conservative same-plan entry-mirror loss bound for successful decode.

This module neither simulates lower caches nor estimates a counterfactual
policy's benefit. The caller must establish the source contract: one decode
batch, distinct GPU misses, no GPU victim in the complete needed set, prior
claims released, and prior same-stream copies complete. The frozen study
also checks top-k=10, L2=16, workers=16, mirror=8 and its captured identities.

The bound applies only to a mirror opportunity lost before its expert's
source claim in the same plan. It does not bound indirect history effects,
future read savings, physical storage I/O, exposed waiting, or throughput.
"""

from collections.abc import Iterable, Mapping


def _expert_set(values, label, distinct=False):
    if (not isinstance(values, Iterable)
            or isinstance(values, (Mapping, str, bytes, bytearray))):
        raise ValueError(f"{label} must be an iterable of expert IDs")
    result = set()
    for expert in values:
        if type(expert) is not int or expert < 0:
            raise ValueError(f"invalid {label} expert ID")
        if distinct and expert in result:
            raise ValueError(f"duplicate {label} expert ID")
        result.add(expert)
    return result


def plan_direct_loss_upper(missing_ids, occupied_victims,
                           possible_mirror_ids, mirror_k=8):
    """Return an upper bound on direct lost-at-claim mirror opportunities.

    ``missing_ids`` and ``occupied_victims`` contain distinct nonnegative
    integer IDs. Filter empty GPU slots (-1) from occupied_victims first;
    this list need not have one element per miss. Victims must be disjoint
    from missing_ids. The caller additionally checks disjointness from all
    needed IDs, including GPU hits, which are not arguments to this function.

    ``possible_mirror_ids`` is an overapproximation, not a capacity-limited
    ring: start with the observed decode-entry mirror IDs and include every
    occupied GPU victim from strictly earlier plans. Compute this bound
    before adding current victims. Duplicate possible IDs are harmless.
    Do not trim this set to the last K victims: skipped writebacks can retain
    older entry identities. No special first-plan L2 subtraction is applied.

    Four independent limits apply: at most K entry mirrors; at least one
    successful claim precedes any commit-caused loss; each occupied GPU
    overwrite can reserve at most one target; and a lost identity must be
    missing and possibly mirrored at entry. Current GPU victims are outside
    needed, so publication cannot create a new needed mirror, and duplicate
    victim invalidation cannot destroy an additional needed mirror.

    Input bounds such as top-k and expert count belong to the caller's
    frozen capture contract. Arguments are consumed but never modified.
    """
    if type(mirror_k) is not int or mirror_k < 0:
        raise ValueError("mirror_k must be a nonnegative integer")
    missing = _expert_set(missing_ids, "missing", distinct=True)
    victims = _expert_set(occupied_victims, "occupied victim", distinct=True)
    possible = _expert_set(possible_mirror_ids, "possible mirror")
    if len(victims) > len(missing):
        raise ValueError("more occupied victims than GPU misses")
    if victims & missing:
        raise ValueError("occupied victims intersect missing experts")
    return min(mirror_k, max(0, len(missing) - 1), len(victims),
               len(missing & possible))
