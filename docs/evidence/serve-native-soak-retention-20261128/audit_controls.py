"""Exercise the NVML time and process joins with genuine native short records."""
import json
from pathlib import Path
from audit_nvml import audit, check_scans, records

def exercise(root):
    good = audit(root)
    ev = list(records(root/'capture/events.jsonl'))
    q = list(records(root/'capture/nvml-queries.jsonl'))
    r = [row for row in records(root/'execution/status.jsonl') if row.get('business_ready')]
    pid = good['model_pid']
    def check(e, queries, expected_pid=pid):
        return check_scans(e, queries, expected_pid, r[0]['monotonic'], r[-1]['monotonic'])
    def rejects(test, e, queries, expected_pid=pid):
        try:
            check(e, queries, expected_pid)
        except ValueError as err:
            return str(err)
        raise AssertionError(test)
    edits = {}
    edits['missing NVML query'] = rejects('missing', ev, q[1:])
    edits['missing model PID'] = rejects('model', ev, q, -1)
    edits['damaged NVML worker binding'] = rejects('binding', ev, [q[0]|{'request_id':'other'},*q[1:]])
    edits['query summary conflicts with worker'] = rejects('summary', ev, [q[0]|{'pids':[pid]},*q[1:]])
    edits['query clock regression'] = rejects('clock', ev, [q[1],q[0],*q[2:]])
    edits['incomplete lease'] = rejects('release', ev[:-1], q)
    require_monitor = next(i for i,e in enumerate(ev) if e['event']=='monitor')
    early = ev[require_monitor] | {'monotonic': ev[require_monitor-1]['monotonic']}
    edits['reply outside scan interval'] = rejects('interval', ev[:require_monitor]+[early]+ev[require_monitor+1:], q)
    # inspect() may ask a second time when an owned CUDA process exits.
    raw = [json.loads(s) for s in q[0]['stdout'].splitlines()]
    extra_id = '0' * 32
    when = raw[-1]['monotonic']
    preflight = next(e['monotonic'] for e in ev if e['event']=='preflight')
    assert when < preflight
    for v in raw:
        v['request_id'] = extra_id
    raw[-1]['monotonic'] = (when+preflight)/2
    additional = q[0] | {'request_id': extra_id,'stdout':'\n'.join(map(json.dumps,raw))+'\n'}
    two = check(ev, [q[0], additional, *q[1:]])
    assert two['guard_scans']==good['guard_scans'] and two['additional_queries']==good['additional_queries']+1
    return {'recorded_short':good,'rejected':edits,'second_query_control':'accepted'}

if __name__ == '__main__':
    import sys
    print(json.dumps(exercise(Path(sys.argv[1])), indent=2))
