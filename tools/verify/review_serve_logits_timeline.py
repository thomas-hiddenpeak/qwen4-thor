"""Compare ordered CUDA kernel signatures per stream; save every difference."""
import argparse
import json
from pathlib import Path
import sqlite3


def kernels(path):
    with sqlite3.connect(path) as db:
        rows = db.execute('select k.streamId,s.value,k.gridX,k.gridY,k.gridZ,'
                          'k.blockX,k.blockY,k.blockZ from CUPTI_ACTIVITY_KIND_KERNEL k '
                          'join StringIds s on s.id=k.demangledName order by k.start').fetchall()
    streams = {}
    for stream, *signature in rows:
        streams.setdefault(stream, []).append(tuple(signature))
    return streams


def compare(old, new):
    i = j = 0
    changes = []
    while i < len(old) and j < len(new):
        if old[i] == new[j]:
            i += 1
            j += 1
            continue
        anchors = []
        for di in range(min(65, len(old)-i)):
            for dj in range(min(65, len(new)-j)):
                if old[i+di:i+di+4] == new[j+dj:j+dj+4]:
                    anchors.append((di+dj, di, dj))
        if not anchors and max(len(old)-i, len(new)-j) <= 64:
            changes.append({'old_index':i, 'new_index':j,
                            'removed':old[i:], 'added':new[j:]})
            return changes
        if not anchors:
            raise RuntimeError(f'No local alignment at old={i}, new={j}; requires manual review')
        _, di, dj = min(anchors)
        changes.append({'old_index':i, 'new_index':j,
                        'removed':old[i:i+di], 'added':new[j:j+dj]})
        i += di
        j += dj
    if i != len(old) or j != len(new):
        changes.append({'old_index':i,'new_index':j,'removed':old[i:],'added':new[j:]})
    return changes


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--parent', type=Path, required=True)
    ap.add_argument('--candidate', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    a = ap.parse_args()
    root = Path(__file__).resolve().parents[2]
    assert any(a.output.resolve().is_relative_to(root/p) for p in ['build','.q4t-work'])
    assert not a.output.exists()
    old, new = kernels(a.parent), kernels(a.candidate)
    assert old.keys() == new.keys(), 'stream identity needs manual mapping'
    results = []
    for stream in old:
        left, right = old[stream], new[stream]
        # Embed launches delimit forwards and prevent matching a head mixer
        # against the identically named first mixer of the next forward.
        starts = [[0] + [i for i, k in enumerate(rows)
                         if i and '::EmbedLookupKernel(' in k[0]] + [len(rows)]
                  for rows in [left, right]]
        assert len(starts[0]) == len(starts[1]), 'forward counts differ'
        changes = []
        for block in range(len(starts[0])-1):
            oi, oe = starts[0][block:block+2]
            ni, ne = starts[1][block:block+2]
            for change in compare(left[oi:oe], right[ni:ne]):
                change['old_index'] += oi
                change['new_index'] += ni
                change['forward_segment'] = block
                changes.append(change)
        results.append({'stream':stream,'old_count':len(old[stream]),
                        'new_count':len(new[stream]),'changes':changes})
    a.output.write_text(json.dumps({'streams':results,'review_pending':True,
                                   'limits':'Kernel names/grid/block/order only; pointers and scalar arguments are not captured.'},indent=2)+'\n')
    print([(x['stream'],x['old_count'],x['new_count'],len(x['changes'])) for x in results])


if __name__ == '__main__':
    main()
