"""Post-gate multi-slot allocation observations with real HTTP output checks."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'verify'))
from serve_logits_gate import ROOT, require_http
from run_prefill_consumers import parse


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--accepted-root', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    a = ap.parse_args()
    run, out = a.accepted_root.resolve(), a.output.resolve()
    assert any(out.is_relative_to(ROOT/p) for p in ['build','.q4t-work'])
    require_http(run)
    assert json.loads((run/'consumer-timelines/complete.json').read_text())['captures']==8
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out)
    baseline = run/'consumers-inline/candidate'
    summaries=[]
    for slots in [2,4]:
        directory=out/str(slots)
        directory.mkdir()
        command=json.loads((baseline/'server-command.json').read_text())
        command[command.index('--max-seq')+1]=str(slots)
        env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
        env['LD_PRELOAD']=str(run/'consumer-timelines/alloc.so')
        (directory/'command.json').write_text(json.dumps(command,indent=2)+'\n')
        (directory/'environment.json').write_text(json.dumps({'LD_PRELOAD':env['LD_PRELOAD']})+'\n')
        with (directory/'server.log').open('w') as log:
            server=subprocess.Popen(command,env=env,stdout=log,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+240
            while 'serving on port' not in (directory/'server.log').read_text():
                assert server.poll() is None and time.monotonic()<deadline
                time.sleep(1)
            startup=(directory/'server.log').read_text()
            assert f'=> max_len=65536 max_seq={slots}' in startup
            observations=[]
            for b,status,extent,query,offset in re.findall(r'\[logits-alloc\] bytes=(\d+) status=(\d+) extent=(\d+) query=(-?\d+) caller_offset=([0-9a-f]+)',startup):
                if int(b)!=slots*496640: continue
                symbol=subprocess.check_output(['addr2line','-f','-C','-e',str(ROOT/'build/q4t'),'0x'+offset],text=True)
                assert status==query=='0' and int(extent)==int(b)
                assert 'ChatServer::Start' in symbol
                observations.append({'bytes':int(b),'extent':int(extent),'caller':symbol.strip(),'offset':offset})
            assert len(observations)==2 and len({x['offset'] for x in observations})==2
            client=json.loads((baseline/'1024/command.json').read_text())
            case=directory/'http';case.mkdir()
            client[client.index('--number')+1]='1'
            client[client.index('--outputs-dir')+1]=str(case)
            (case/'command.json').write_text(json.dumps(client,indent=2)+'\n')
            with (case/'client.log').open('w') as log:
                subprocess.run(client,stdout=log,stderr=subprocess.STDOUT,check=True)
            payload=json.loads((run/'consumers-inline/1024.jsonl').read_text())
            row,=parse(case,payload,expected_count=1)
            expected=json.loads((baseline/'1024/parsed.json').read_text())[0]
            for field in ['prompt_tokens','completion_tokens','output_sha256']:
                assert row[field]==expected[field]
            summaries.append({'max_seq':slots,'allocations':observations,'http_output_matched':True})
        finally:
            server.terminate()
            try: server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server.kill();server.wait()
            (directory/'exit.json').write_text(json.dumps({'server':server.returncode})+'\n')
        assert server.returncode==0
        print('max_seq',slots,'allocation and HTTP checks passed',flush=True)
    (out/'review.json').write_text(json.dumps({'accepted':True,'cases':summaries,
        'limits':'Observed allocation ranges and one HTTP request per capacity. Actual concurrent correctness uses separate B=2/lifecycle gates. No system peak/residency claim.'},indent=2)+'\n')


if __name__=='__main__':
    main()
