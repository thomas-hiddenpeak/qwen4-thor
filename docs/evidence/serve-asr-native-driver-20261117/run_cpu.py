import ast,hashlib,json,pathlib,runpy,subprocess,time,traceback
root=pathlib.Path.cwd();out=root/'.q4t-work/serve-audio-native-20261117/native-driver-cpu-01';out.mkdir()
files=['tools/native_asr/service_acceptance.py','tools/native_asr/transcription_sdk.py','tools/native_asr/transcription_sdk.mjs']
identity={n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in files}
for n in files:
 p=out/'source'/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((root/n).read_bytes())
(out/'source-identity.json').write_text(json.dumps(identity,indent=2)+'\n')
(out/'plan.json').write_text(json.dumps(dict(tests=['retained-real-sse','broken-terminal-or-delta'],seconds=30,gpu_actions=0),indent=2)+'\n')
start=time.monotonic();r=dict(status='FAILED',tests=2,gpu_actions=0)
try:
 for n in files[:2]:ast.parse((root/n).read_text(),filename=n)
 node=subprocess.run(['node','--check',files[2]],capture_output=True,text=True,timeout=10)
 assert node.returncode==0,(node.stderr,node.stdout)
 m=runpy.run_path(files[0]);wire=(root/'docs/evidence/serve-asr-http-20261117/service-http-05/js-en-stream.wire').read_bytes()
 actual=m['stream_value'](wire);assert m['direct']().quality(actual['text'],'en')['exact']
 events=[json.loads(l[6:]) for l in wire.decode().splitlines() if l.startswith('data: {')]
 mutations=[events[:-1],events+[events[-1]],[dict(events[0],delta='wrong'),*events[1:]]]
 for rows in mutations:
  changed=''.join('data: '+json.dumps(row)+'\n\n' for row in rows).encode()
  try:m['stream_value'](changed)
  except RuntimeError:pass
  else:raise AssertionError('accepted broken terminal/delta')
 r.update(status='PASS',negative_controls=len(mutations),actual_text=actual['text'],syntax='PASS')
except BaseException:r['error']=traceback.format_exc()
r['seconds']=time.monotonic()-start
if r['seconds']>30:r['status']='FAILED'
(out/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False));raise SystemExit(0 if r['status']=='PASS' else 1)
