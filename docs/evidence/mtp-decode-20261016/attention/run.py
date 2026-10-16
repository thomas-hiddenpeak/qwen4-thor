import json, subprocess, statistics
from pathlib import Path
root = Path('/home/rm01/models/dev/qwen4-thor')
out = Path(__file__).resolve().parent
prior = root / '.q4t-work/mtp-numerical-closure-20261016/http'
common = [str(root/'tools/evalscope/.venv/bin/python3'), 'tools/evalscope/run_acceptance.py', '--model-dir', '/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream', '--startup-timeout', '300', '--allow-unqualified-binary']
comparisons = {}
for name, mode, mtp in [('quality-on','quality',True), ('on','performance',True), ('off','performance',False), ('quality-off','quality',False)]:
    fixture = prior/name/('inputs' if mode == 'quality' else '')
    cmd = common + ['--output', str(out/name), '--mode', mode, '--fixtures', str(fixture), '--reference', str(prior/name/'results.json'), '--port', '8731' if mtp else '8730']
    if mtp: cmd += ['--mtp-default']
    with (out/(name+'-driver.log')).open('w') as log:
        rc = subprocess.run(cmd, cwd=root, stdout=log, stderr=subprocess.STDOUT).returncode
    (out/(name+'-exit.json')).write_text(json.dumps({'returncode':rc,'command':cmd},indent=2)+'\n')
    if rc: raise SystemExit(rc)
    if mode != 'performance': continue
    before = json.loads((prior/name/'results.json').read_text())
    after = json.loads((out/name/'results.json').read_text())
    rows=[]
    for b,a in zip(before,after):
        assert a['length']==b['length'] and a['outputs']==b['outputs'] and a['prompt_sha256']==b['prompt_sha256']
        bt=[m['decode_tps'] for m in b['metrics']]; at=[m['decode_tps'] for m in a['metrics']]
        bf=[m['ttft'] for m in b['metrics']]; af=[m['ttft'] for m in a['metrics']]
        rows.append(dict(length=a['length'], before_tps=statistics.harmonic_mean(bt), after_tps=statistics.harmonic_mean(at), gain=statistics.harmonic_mean(at)/statistics.harmonic_mean(bt)-1, before_tps_samples=bt, after_tps_samples=at, before_ttft_samples=bf, after_ttft_samples=af, decode_regression=max(at)<min(bt), ttft_regression=min(af)>max(bf)))
    comparisons[name]=rows
    (out/'comparison.json').write_text(json.dumps(comparisons,indent=2)+'\n')
    if any(r['decode_regression'] or r['ttft_regression'] for r in rows):
        (out/'gate-stop.json').write_text(json.dumps({'mode':name,'reason':'non-overlapping adverse performance range; retain evidence, no extra samples'},indent=2)+'\n')
        raise SystemExit(2)
