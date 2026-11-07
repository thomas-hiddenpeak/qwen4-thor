"""Offline check of actual image/tool responses and final installed pair."""
import io
import json
import fcntl
import socket
from pathlib import Path
from tools.vision_service import rowmax_delivery as d
from tools.evalscope import resume_vision_rowmax as r
s,h=d.s,d.h
out=d.HEALTH/'execution'
plan=h.read(d.HEALTH/'plan.json')
result=h.read(out/'result.json');guard=h.read(d.HEALTH/'lease/exit.json')
s.require(result['status']==d.Run.success_status and guard['status']=='COMPLETED'
    and guard['child_returncode']==0 and not result['failure']
    and not result['cleanup_errors'] and not result['monitor_failures'],'health stage')
c=result['counts'];s.require(c['starts']==1 and c['generations']==4 and c['native']==0
    and 5<=c['controls']<=16 and result['seconds']<300,'health counts')
events=r.jsonlines(d.HEALTH/'lease/events.jsonl');queries=r.jsonlines(d.HEALTH/'lease/nvml-queries.jsonl')
life=r.verify_lifetime(out,'model-health',events,queries)
s.require(all(q['status']=='OK' and set(q['pids'])<={life['pid']} for q in queries)
    and queries[-1]['pids']==[],'health observations')
s.require(all(not e['conflicts'] and not e['unreadable_processes'] for e in events
    if e['event']=='monitor'),'health competition')
labels=['health-image-false','health-image-true','health-tool-call','health-tool-final']
parsed={}
for label in labels:
 request=h.read(out/(label+'-request.json'))['body']
 transport=h.read(out/(label+'-transport.json'))
 s.require(transport['status']==200,'HTTP error')
 body=(out/(label+'-wire.bin')).read_bytes()
 value=(s.client.read_sse if request.get('stream') else s.client.read_json)(io.BytesIO(body))
 s.require(value.id==request['request_id'] and value.model==request['model'],'response ID')
 parsed[label]=value
fixture=h.read(d.HEALTH/'image.json')
for label in labels[:2]:
 value=parsed[label];actual=json.loads(value.message['content'])
 s.Draft202012Validator(fixture['request']['response_format']['json_schema']['schema']).validate(actual)
 s.require(value.finish_reason=='stop' and actual==fixture['expected'],'image answer')
call=parsed['health-tool-call'];calls=s.client.prepare_tool_calls(call,s.VALIDATORS)
s.require(len(calls)==1 and calls[0].arguments==dict(path='docs/STATUS.md',start_line=1,line_count=16),'tool call')
record=h.read(out/'real-tool-result.json')
s.require(record['ok'] and not record['source']['truncated'] and record['source']['sha256']==plan['files'][str(s.ROOT/'docs/STATUS.md')],'real record identity')
final=parsed['health-tool-final']
s.require(final.finish_reason=='stop' and record['source']['citation'] in final.message['content'],'real citation')
for route in ('health-healthz','health-readyz'):
 s.require(h.read(out/(route+'-transport.json'))['status']==200,'health endpoint')
metrics=s.parse_metrics(sorted(out.glob('health-idle-*-wire.bin'))[-1].read_text())
s.require(metrics['q4t_requests_total']==metrics['q4t_requests_success_total']==4
    and metrics['q4t_requests_error_total']==metrics['q4t_requests_aborted_total']==0,'health outcomes')
pids={life['pid']}|{q['worker_pid'] for q in queries}
for e in events:
 pids.add(e['pid'])
 if 'child_pid' in e:pids.add(e['child_pid'])
s.require(all(not Path(f'/proc/{pid}').exists() for pid in pids),'owned PID remains')
with (s.ROOT/'.q4t-work/gpu-exclusive.lock').open() as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 with socket.socket() as sock:
  sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
  sock.bind(('127.0.0.1',plan['port']));sock.listen(1)
verified=d.verify();receipt=h.read(d.BASE/'deployment-finalized.json')
s.require(verified['installed']==verified['pair']['new']==receipt['pair']
    and not d.MARKER.exists(),'final pair')
report=dict(status='PASS_FINAL_MAINLINE_IMAGE_TOOL_DELIVERY',counts=c,
 seconds=result['seconds'],nvml_queries=len(queries),owned_pids_reaped=len(pids),
 image_json_sse=True,real_readonly_tool_citation=True,healthy_endpoints=True,
 installed=verified['installed'],rollback=receipt['rollback'],gpu_empty=True,
 port_free=True,lock_available=True,persistent_service=False,
 inherited_limits='video temporal quality, hardware decoding, concurrency, soak and sealed managed package identities unchanged')
h.save(d.BASE/'delivery-audit.json',report)
print(json.dumps(report,ensure_ascii=False))
