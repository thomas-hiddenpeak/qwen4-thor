"""Sweep existing CUDA activity intervals, without new GPU execution."""
import collections
import heapq
import json
from pathlib import Path
import sqlite3


def sweep(intervals, begin, end):
    cursor = begin
    heap = []
    active = {}
    cardinality = collections.Counter()
    exclusive = collections.Counter()
    combinations = collections.Counter()
    fully_solo = collections.defaultdict(lambda: {'count': 0, 'duration_ns': 0})
    operation_counts = collections.Counter()

    def advance(target):
        nonlocal cursor
        assert target >= cursor
        dt = target - cursor
        cardinality[len(active)] += dt
        if len(active) == 1:
            exclusive[next(iter(active.values()))[0]] += dt
        elif len(active) > 1 and dt:
            names = tuple(sorted(v[0] for v in active.values()))
            combinations[names] += dt
            for value in active.values():
                value[1] = True
        cursor = target

    def finish_until(target):
        while heap and heap[0][0] <= target:
            t = heap[0][0]
            advance(t)
            while heap and heap[0][0] == t:
                _, key = heapq.heappop(heap)
                name, overlapped, duration = active.pop(key)
                if not overlapped:
                    fully_solo[name]['count'] += 1
                    fully_solo[name]['duration_ns'] += duration
        advance(target)

    for key, (a, b, name) in enumerate(intervals):
        assert begin <= a < b <= end
        finish_until(a)
        active[key] = [name, False, b-a]
        operation_counts[name] += 1
        heapq.heappush(heap, (b, key))
    finish_until(end)
    assert not active and sum(cardinality.values()) == end-begin
    return {'cardinality_ns': dict(cardinality),
            'exclusive_ns': dict(exclusive),
            'fully_solo': dict(fully_solo),
            'operation_counts': dict(operation_counts),
            'overlap_sets': [{'names': list(k), 'ns': v}
                             for k,v in sorted(combinations.items(), key=lambda x:-x[1])]}


def checks():
    # Touching endpoints do not overlap; nested operations and duplicate names
    # still count as distinct operations. The expected wall partition is manual.
    x = sweep([(0,10,'a'), (2,5,'b'), (5,8,'b'), (10,12,'c')],0,15)
    assert x['cardinality_ns'] == {0:3, 1:6, 2:6}
    assert x['exclusive_ns'] == {'a':4, 'c':2}
    assert x['fully_solo'] == {'c':{'count':1,'duration_ns':2}}
    x = sweep([(0,4,'a'),(0,4,'a')],0,4)
    assert x['cardinality_ns'][2] == 4 and not x['fully_solo']


def main():
    checks()
    out = Path(__file__).resolve().parent
    source = out.parent/'hc-up-clean-trace-v2-20261017'
    prior = json.loads((source/'device-audit.json').read_text())
    result = {}
    for v in ['A','B']:
        c = sqlite3.connect(f'file:{source/v/"context-204800/trace.sqlite"}?mode=ro', uri=True)
        c.execute('pragma temp_store=MEMORY')
        names = dict(c.execute('select id,value from StringIds'))
        begin=prior[v]['chunks'][0]['start_ns'];end=prior[v]['chunks'][-1]['end_ns']
        sql = '''select start,end,demangledName from CUPTI_ACTIVITY_KIND_KERNEL where end<=? and end>?
                 union all select start,end,-1 from CUPTI_ACTIVITY_KIND_MEMCPY where end<=? and end>?
                 union all select start,end,-2 from CUPTI_ACTIVITY_KIND_MEMSET where end<=? and end>?
                 order by start'''
        names[-1]='MEMCPY';names[-2]='MEMSET'
        rows=((max(a,begin),b,names[n]) for a,b,n in c.execute(sql,(end,begin)*3))
        result[v]=sweep(rows,begin,end)
        cards=result[v]['cardinality_ns']
        assert cards.get(0,0)==prior[v]['summary']['all']['gaps_ns']
        assert sum(t for n,t in cards.items() if n)>0
        assert sum(t for n,t in cards.items() if n)==prior[v]['summary']['all']['busy_ns']
        assert sum(result[v]['exclusive_ns'].values())==cards.get(1,0)
        c.close()
        print(v, cards, flush=True)
    result['scope']='Process-local activity cardinality; fully_solo operations never overlap any traced CUDA activity. Untraced activity and profiler perturbation are not excluded. No counterfactual causality.'
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')


if __name__=='__main__':
    main()
