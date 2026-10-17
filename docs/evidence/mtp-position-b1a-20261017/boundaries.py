import hashlib,json,os,subprocess,time,threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from http.client import HTTPConnection
root=Path('/home/rm01/models/dev/qwen4-thor')
out=Path(__file__).resolve().parent/'boundaries'
out.mkdir(exist_ok=False)
prompt=json.loads((root/'.q4t-work/mtp-merge-e2e-20261015/on/context-1024/requests.jsonl').read_text().splitlines()[0])['prompt']
cmd=[str(Path(__file__).resolve().parent/'q4t-candidate'),'serve','--model-dir','/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream','--port','8734','--max-seq','1','--max-prefill','8192','--max-len','1040']
(out/'command.json').write_text(json.dumps(cmd,indent=2))
(out/'binary.sha256').write_text(hashlib.sha256((Path(__file__).resolve().parent/'q4t-candidate').read_bytes()).hexdigest())
env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
results=[]
cancel_result={}
with (out/'server.log').open('w') as log:
    server=subprocess.Popen(cmd,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT)
    try:
        until=time.monotonic()+300
        while 'serving on port' not in (out/'server.log').read_text():
            if server.poll() is not None or time.monotonic()>until: raise RuntimeError('startup failed')
            time.sleep(.2)
        for index,(maximum,stream) in enumerate([(1,False),(2,True),(4,False),(5,True),(64,True),(1,False)]):
            if index==5:
                first=threading.Event()
                def consume():
                    conn=HTTPConnection("127.0.0.1",8734,timeout=30)
                    body={"model":"qwen3.8-flash-next","prompt":prompt,"max_tokens":64,"temperature":0,"stream":True,"request_id":"boundary-cancel","cancel_token":"a"*64}
                    conn.request("POST","/v1/chat/completions",json.dumps(body),{"Content-Type":"application/json"})
                    response=conn.getresponse();lines=[]
                    for line in response:
                        lines.append(line.decode())
                        if line.startswith(b"data: {"):
                            event=json.loads(line[6:])
                            if any(c.get("delta",{}).get("content") for c in event.get("choices",[])):first.set()
                    conn.close()
                    return {"status":response.status,"raw":"".join(lines)}
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending=pool.submit(consume)
                    assert first.wait(30),"no streaming output before cancellation"
                    conn=HTTPConnection("127.0.0.1",8734,timeout=30)
                    conn.request("POST","/v1/requests/cancel",json.dumps({"request_id":"boundary-cancel","cancel_token":"a"*64}),{"Content-Type":"application/json"})
                    response=conn.getresponse();cancel_status=response.status;cancel_body=response.read().decode();conn.close()
                    cancel_result={"cancel_status":cancel_status,"cancel_body":cancel_body,"response":pending.result(timeout=30)}
                    (out/"cancellation.json").write_text(json.dumps(cancel_result,indent=2))
                    assert cancel_status==202,cancel_result
                    assert cancel_result["response"]["status"]==200 and '"error"' in cancel_result["response"]["raw"],cancel_result
                    assert '"finish_reason":"length"' not in cancel_result["response"]["raw"],cancel_result
                    cancel_result["passed"]=True
                    (out/"cancellation.json").write_text(json.dumps(cancel_result,indent=2))
            payload={'model':'qwen3.8-flash-next','prompt':prompt,'max_tokens':maximum,'temperature':0,'seed':20260920,'stream':stream}
            if stream: payload['stream_options']={'include_usage':True}
            conn=HTTPConnection('127.0.0.1',8734,timeout=300)
            conn.request('POST','/v1/chat/completions',json.dumps(payload),{'Content-Type':'application/json'})
            response=conn.getresponse(); raw=response.read().decode(); status=response.status; conn.close()
            record={'request':payload,'http_status':status,'raw':raw}; results.append(record)
            (out/'responses.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
            assert status==200,(maximum,status,raw)
            if stream:
                events=[json.loads(line[6:]) for line in raw.splitlines() if line.startswith('data: ') and line[6:]!='[DONE]']
                assert 'data: [DONE]' in raw
                usage=next(e['usage'] for e in events if e.get('usage'))
                finish=[c['finish_reason'] for e in events for c in e.get('choices',[]) if c.get('finish_reason')]
            else:
                event=json.loads(raw);usage=event['usage'];finish=[event['choices'][0]['finish_reason']]
            expected=min(maximum,1040-usage['prompt_tokens'])
            assert usage['completion_tokens']==expected,(maximum,usage,expected)
            assert finish==['length'],finish
            record.update({'expected_output':expected,'usage':usage,'finish':finish,'passed':True})
            (out/'responses.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    finally:
        server.terminate()
        try: code=server.wait(timeout=120)
        except subprocess.TimeoutExpired:
            server.kill();server.wait();raise
        (out/'exit.json').write_text(json.dumps({'server_returncode':code,'passed':len(results)==6 and all(x.get('passed') for x in results) and cancel_result.get('passed',False)}))
print('boundary requests passed:',len(results))
