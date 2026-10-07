"""Lower bounds from existing offload diagnostics; no new model execution.

If the previous sub-chunk requires P and the next requires N, a C-slot cache
can retain at most C-|P| other experts. Thus at least
max(0, |N-P|-(C-|P|)) GPU loads are unavoidable at that transition, regardless
of the victim policy. This is a lower bound, not a whole-request optimum.
"""
import argparse
import json
from pathlib import Path
import re

from analyze import sha


FLUSH = re.compile(r"\[residency\]\[diag\] layer=(\d+) flush size=(\d+) "
                   r"book=(\d+) actual=(\d+)")
CHUNKS = re.compile(r"\[residency\]\[diag\] layer=(\d+) T=(\d+) D=(\d+) "
                    r"chunks=(\d+) max_distinct=(\d+) resident_before=(\d+) "
                    r"overlap=([\d,]*) new=([\d,]*)")


def transition_bound(previous_size, next_new, capacity):
    if not 0 <= previous_size <= capacity or not 0 <= next_new <= capacity:
        raise ValueError("invalid set/capacity")
    return max(0, next_new - (capacity - previous_size))


def analyze_log(path):
    pending, layers, requests = [], [], []
    for line in Path(path).read_text().splitlines():
        match = FLUSH.search(line)
        if match:
            layer, rows, book, actual = map(int, match.groups())
            if book != actual:
                raise ValueError("inconsistent distinct bookkeeping")
            pending.append((layer, rows, actual))
            continue
        match = CHUNKS.search(line)
        if match:
            layer, rows, cap, count, max_distinct, resident = map(
                int, match.groups()[:6])
            overlaps, new = [[int(x) for x in part.split(',') if x]
                             for part in match.groups()[6:]]
            if (len(pending) != count or len(new) != count - 1 or
                    len(overlaps) != count - 1 or
                    sum(v[1] for v in pending) != rows or
                    any(v[0] != layer for v in pending) or
                    max(v[2] for v in pending) != max_distinct or
                    max_distinct > cap or resident > cap):
                raise ValueError("incomplete or inconsistent chunk diagnostics")
            lower = {"runtime_prefill": 0, "runtime_decode": 0}
            for j in range(1, count):
                if (overlaps[j - 1] + new[j - 1] != pending[j][2] or
                        overlaps[j - 1] > pending[j - 1][2]):
                    raise ValueError("inconsistent overlap")
                phase = ("runtime_decode" if pending[j][1] == 1
                         else "runtime_prefill")
                lower[phase] += transition_bound(
                    pending[j - 1][2], new[j - 1], cap)
            layers.append(dict(layer=layer, rows=rows, chunks=count,
                               adjacent_new=sum(new), **lower))
            pending = []
        elif "[q4t][residency] id=" in line:
            fields = dict(re.findall(r"([\w]+)=([^ ]+)", line))
            if not layers or pending:
                raise ValueError("missing request diagnostics")
            lower = {key: sum(v[key] for v in layers)
                     for key in ("runtime_prefill", "runtime_decode")}
            pmiss, dmiss = int(fields["pmiss"]), int(fields["dmiss"])
            if lower['runtime_prefill'] > pmiss or lower['runtime_decode'] > dmiss:
                raise ValueError("lower bound exceeds observed GPU loads")
            requests.append(dict(
                request_id=fields['id'], input_tokens=int(fields['in']),
                output_tokens=int(fields['out']), observed_prefill_misses=pmiss,
                observed_decode_misses=dmiss, transition_lower_bounds=lower,
                prefill_fixed_order_avoidable_loads_upper_bound=(
                    pmiss - lower['runtime_prefill']),
                prefill_fixed_order_avoidable_fraction_upper_bound=(
                    1 - lower['runtime_prefill'] / pmiss if pmiss else 0),
                layer_calls=len(layers), subchunks=sum(v['chunks'] for v in layers),
                first_chunk_and_cross_forward_bounds_not_counted=True))
            layers = []
    if pending or layers or not requests:
        raise ValueError("incomplete request or no residency summary")
    return dict(source=str(path), sha256=sha(Path(path)), requests=requests,
                scope="GPU loads under unchanged chunk order; not SSD bytes")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--log', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[2]
    out = args.output.resolve()
    if not any(out.is_relative_to(root / p) for p in ['build', '.q4t-work']):
        ap.error('output must be under build/ or .q4t-work/')
    result = analyze_log(args.log)
    with out.open('x') as file:
        json.dump(result, file, indent=2)
        file.write('\n')


if __name__ == '__main__':
    main()
