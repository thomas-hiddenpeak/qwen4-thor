"""Audit terminal control-budget evidence without turning samples into SLOs."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[3]
CONTROL = ROOT / '.q4t-work/control-budget-control-20260927'
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    assert not CONTROL.exists()
    names = ['control-budget-baseline', 'control-budget-soak', 'control-budget-baseline-replay', 'control-budget-proxy']
    dirs = [ROOT / ('.q4t-work/e2e/' + n + '-20260927') for n in names]
    plans = [json.loads((d / 'plan.json').read_text()) for d in dirs]
    completed = json.loads((ROOT / '.q4t-work/prepared/control-finish-exits-20260927.json').read_text())
    assert len(completed) == 2 and all(x['returncode'] == 0 for x in completed)
    assert sha(ROOT / 'tools/deploy/nginx/q4t.conf.in') == plans[1]['template_sha256'] == plans[3]['template_sha256']
    digest = plans[0]['binary_sha256']
    assert all(p['binary_sha256'] == digest for p in plans)
    for d in dirs[:3]:
        e = json.loads((d / 'exit.json').read_text())
        assert e['failure'] is None and e['proxy'] == 0
        assert e['server_exits'] and all(x == 0 for x in e['server_exits'])
    e = json.loads((dirs[-1] / 'exit.json').read_text())
    assert e['failure'] is None and e['server'] == e['proxy'] == 0 and len(e['records']) == 7
    binary = Path(plans[0]['command'][0]); nginx = Path(plans[0]['nginx'][0])
    assert sha(binary) == digest
    assert sha(nginx) == plans[0]['nginx_sha256']
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit(): continue
        try:
            args = (proc / 'cmdline').read_bytes().split(b'\0')
            exe = (proc / 'exe').resolve(strict=True)
        except OSError: continue
        assert not (exe == binary and b'serve' in args)
        assert exe != nginx
        assert not any(Path(a.decode(errors='replace')).name in ['run_control_soak.py', 'run_proxy_admission.py', 'finish-control-budget-20260927.py'] for a in args)
    assert all(json.loads((d / 'metrics-saturated-cancel.json').read_text())['status'] == 429 for d in [dirs[0], dirs[2]])
    soak = dirs[1]
    assert json.loads((soak / 'metrics-saturated-cancel.json').read_text())['status'] == 404
    assert json.loads((soak / 'metrics-full-active-response.json').read_text())['status'] == 409
    assert json.loads((soak / 'cancel-saturated-response.json').read_text())['status'] == 429
    assert json.loads((soak / 'cancel-timeout-recovery.json').read_text())['seconds'] <= 15
    assert json.loads((soak / 'backend-down-response.json').read_text())['status'] == 502
    assert json.loads((soak / 'restart-active-response.json').read_text())['status'] in [409, 502]
    duration = json.loads((soak / 'soak.json').read_text())
    assert duration['cycles'] >= 12 and 600 <= duration['seconds'] <= 810
    resources = json.loads((soak / 'resources.json').read_text())
    assert len(resources) == duration['cycles']
    assert max(x['fd_count'] for x in resources) - min(x['fd_count'] for x in resources) <= 1
    assert len({x['Threads'] for x in resources}) == 1
    normal = [(soak, f'cycle-{i}-{kind}', '711146' if kind == 'long' else '710003', 45056 if kind == 'long' else 1024) for i in range(duration['cycles']) for kind in ['long','short']]
    normal += [(soak, 'restart-recovery', '710003', 1024)]
    normal += [(dirs[-1], 'context-200k', '711273', 204800)] + [(dirs[-1], f'recovery-{i}', '710003', 1024) for i in range(3)]
    for d, label, expected, tokens in normal:
        response = json.loads((d / (label + '-response.json')).read_text())
        assert response['status'] == 200 and response['body'].strip().endswith('data: [DONE]')
        events = [json.loads(line[6:]) for line in response['body'].splitlines() if line.startswith('data: {')]
        choices = [c for e in events for c in e.get('choices', [])]
        assert ''.join(c.get('delta', {}).get('content','') for c in choices) == expected
        assert [c['finish_reason'] for c in choices if c.get('finish_reason')] == ['stop']
        usage = next(e['usage'] for e in events if e.get('usage'))
        assert usage['prompt_tokens'] == tokens and usage['completion_tokens'] == 7
    for i in range(duration['cycles']):
        assert json.loads((soak / f'cycle-{i}-cancel-response.json').read_text())['status'] == 409
    CONTROL.mkdir()
    shutil.copy2(ROOT / '.q4t-work/prepared/control-finish-exits-20260927.json', CONTROL / 'finish-exits.json')
    shutil.copy2(binary, CONTROL / 'tested-q4t')
    shutil.copy2(nginx, CONTROL / 'tested-nginx')
    shutil.copytree(Path(__file__).parent, CONTROL / 'tools-final')
    shutil.copytree(ROOT / 'tools/deploy/nginx', CONTROL / 'profile-final')
    for n in names:
        shutil.copy2(ROOT / ('.q4t-work/prepared/' + n + '-20260927.log'), CONTROL / (n + '-driver.log'))
    (CONTROL / 'summary.json').write_text(json.dumps({'cycles': duration['cycles'], 'seconds': duration['seconds'], 'normal_responses_audited': len(normal), 'runner_changed': False, 'strict_non_regression_proven': False}, indent=2))
    binding = {str(f.relative_to(ROOT)): sha(f) for d in [CONTROL, *dirs] for f in sorted(d.rglob('*')) if f.is_file()}
    (CONTROL / 'artifact-binding.json').write_text(json.dumps(binding, indent=2))
    assert all(sha(ROOT / f) == h for f,h in binding.items())
    print(json.dumps({'files': len(binding), 'normal_responses': len(normal), **duration}))


if __name__ == '__main__':main()
