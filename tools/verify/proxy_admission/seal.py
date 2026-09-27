"""Seal the final local proxy qualification after both services and driver exit."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / '.q4t-work/e2e/proxy-admission-v3-20260927'
CONTROL = ROOT / '.q4t-work/proxy-admission-control-20260927'
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    assert not CONTROL.exists()
    result = json.loads((OUT / 'exit.json').read_text())
    assert result['failure'] is None and result['server'] == result['proxy'] == 0
    assert len(result['records']) == 7
    plan = json.loads((OUT / 'plan.json').read_text())
    binary = Path(plan['server'][0])
    nginx = Path(plan['nginx'][0])
    assert sha(binary) == plan['binary_sha256']
    assert sha(nginx) == plan['nginx_sha256']
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = (proc / 'cmdline').read_bytes().split(b'\0')
            exe = (proc / 'exe').resolve(strict=True)
        except OSError:
            continue
        assert not (exe == binary and b'serve' in args)
        assert exe != nginx
        assert not any(Path(x.decode(errors='replace')).name == 'run_proxy_admission.py' for x in args)
    assert json.loads((OUT / 'direct-cap-response.json').read_text())['status'] == 503
    assert json.loads((OUT / 'overload-response.json').read_text())['status'] == 429
    for i in range(4):
        assert json.loads((OUT / f'full-{i}-response.json').read_text())['status'] == 409
    for label, expected, tokens in [('context-200k', '711273', 204800)] + [(f'recovery-{i}', '710003', 1024) for i in range(3)]:
        response = json.loads((OUT / (label + '-response.json')).read_text())
        assert response['status'] == 200 and response['body'].strip().endswith('data: [DONE]')
        events = [json.loads(x[6:]) for x in response['body'].splitlines() if x.startswith('data: {')]
        choices = [c for e in events for c in e.get('choices', [])]
        assert ''.join(c.get('delta', {}).get('content', '') for c in choices) == expected
        assert [c['finish_reason'] for c in choices if c.get('finish_reason')] == ['stop']
        usage = next(e['usage'] for e in events if e.get('usage'))
        assert usage['prompt_tokens'] == tokens and usage['completion_tokens'] == 7
    assert 'shutdown complete (0 in-flight remaining)' in (OUT / 'server.log').read_text()
    CONTROL.mkdir()
    shutil.copy2(binary, CONTROL / 'tested-q4t')
    shutil.copy2(nginx, CONTROL / 'tested-nginx')
    shutil.copy2(__file__, CONTROL)
    for name in ['proxy-admission', 'proxy-admission-v2', 'proxy-admission-v3']:
        shutil.copy2(ROOT / ('.q4t-work/prepared/' + name + '-20260927.log'), CONTROL / (name + '-driver.log'))
    shutil.copytree(ROOT / 'tools/deploy/nginx', CONTROL / 'profile')
    directories = [CONTROL] + [ROOT / ('.q4t-work/e2e/' + name + '-20260927') for name in ['proxy-admission', 'proxy-admission-v2', 'proxy-admission-v3']]
    binding = {str(f.relative_to(ROOT)): sha(f) for d in directories for f in sorted(d.rglob('*')) if f.is_file()}
    (CONTROL / 'artifact-binding.json').write_text(json.dumps(binding, indent=2))
    assert all(sha(ROOT / f) == h for f, h in binding.items())
    print(json.dumps({'files': len(binding), 'normal_http_outputs_audited': 4, 'runner_changed': False, 'five_context_performance_run': False}))


if __name__ == '__main__':
    main()
