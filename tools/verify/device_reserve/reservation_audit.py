"""Independent success ledger for the reservation wrapper's physical pointers."""
import json


def audit_success(lines, plan_sha, sizes):
    reserve = []
    logical = []
    for line in lines:
        if line.startswith('[reserve-trace] '):
            reserve.append(json.loads(line.split(' ', 1)[1]))
        if line.startswith('[memory-trace] '):
            logical.append(json.loads(line.split(' ', 1)[1]))
    plans = [row for row in reserve if row['event'] == 'plan']
    summaries = [row for row in reserve if row['event'] == 'summary']
    prepared = [row for row in reserve if row['event'] == 'prepared']
    assert len(plans) == len(summaries) == len(prepared) == 1
    previous = -1.0
    for sequence, row in enumerate(reserve):
        assert row['seq'] == sequence
        assert row['mono'] >= previous and row['tid'] > 0
        previous = row['mono']
        assert row['event'] in {
            'plan', 'prepared', 'summary', 'handoff', 'alloc', 'free'}
    count = len(sizes)
    assert plans[0]['plan_sha256'] == summaries[0]['plan_sha256'] == plan_sha
    assert plans[0]['count'] == count and plans[0]['total_bytes'] == sum(sizes)
    assert prepared[0]['status'] == summaries[0]['status'] == 'PASS'
    assert prepared[0]['plan_sha256'] == plan_sha
    assert prepared[0]['count'] == prepared[0]['allocated'] == count
    summary = summaries[0]
    assert summary['count'] == summary['consumed'] == summary['freed'] == count
    assert summary['allocated'] == count and summary['error'] == 'none'
    assert summary['unhanded_remaining'] == summary['handed_live'] == 0
    assert summary['release_unknown'] == summary['backend_rc'] == 0
    opened = {}
    pointers = {}
    handed = {}
    freed = {}
    for row in reserve:
        event = row['event']
        if event in ('alloc', 'free'):
            key = (event, row['index'])
            assert 0 <= row['index'] < count
            assert row['bytes'] == sizes[row['index']]
            if row['phase'] == 'begin':
                assert key not in opened
                assert (row['index'] not in pointers if event == 'alloc'
                        else row['index'] in handed and row['index'] not in freed)
                opened[key] = row
                continue
            assert row['phase'] == 'end'
            begin = opened.pop(key)
            assert row['rc'] == 0 and row['mono'] >= begin['mono']
            assert row['tid'] == begin['tid'] and row['bytes'] == begin['bytes']
            if event == 'alloc':
                assert row['ptr'] and row['ptr'] not in {x['ptr'] for x in pointers.values()}
                pointers[row['index']] = row
            else:
                assert row['reason'] == begin['reason'] == 'application'
                assert row['ptr'] == begin['ptr'] == handed[row['index']]['ptr']
                freed[row['index']] = row
        elif event == 'handoff':
            index = row['index']
            assert index not in handed and index in pointers
            assert len(pointers) == count and row['mono'] >= prepared[0]['mono']
            assert row['ptr'] == pointers[index]['ptr']
            assert row['bytes'] == sizes[index]
            assert index == len(handed)
            handed[index] = row
    assert not opened
    assert len(pointers) == len(handed) == len(freed) == count
    assert summary['mono'] >= max(row['mono'] for row in freed.values())
    assert max(row['mono'] for row in pointers.values()) <= prepared[0]['mono']
    pinned = [row for row in logical if row['api'] in ('cudaHostAlloc', 'cudaMallocHost')
              and row['phase'] == 'begin']
    assert pinned and min(row['mono'] for row in pinned) > prepared[0]['mono']
    logical_allocs = [row for row in logical if row['api'] == 'cudaMalloc'
                      and row['phase'] == 'end'][:count]
    assert len(logical_allocs) == count
    for i, row in enumerate(logical_allocs):
        assert row['rc'] == 0 and row['ptr'] == handed[i]['ptr']
        assert row['bytes'] == sizes[i] and row['mono'] >= handed[i]['mono']
    return dict(status='PASS', count=count, bytes=sum(sizes), consumed=count,
                freed=count, plan_sha256=plan_sha,
                all_device_reserved_before_first_pinned=True,
                unhanded_remaining=0, handed_live=0, release_unknown=0)
