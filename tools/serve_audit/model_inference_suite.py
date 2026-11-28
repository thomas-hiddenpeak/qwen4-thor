"""Run one bounded model discovery and inference scenario per pinned SDK."""
import json
import os
from pathlib import Path
import subprocess

from .model_inference_receipt import verify
from .render_sdk_matrix import VERSIONS


def run(out, base, key, certificate, sdks, publication):
    info = json.loads(publication.read_bytes())
    revision = info['publication_revision']
    if info['status'] != 'PASS' or set(sdks) != {f'{lang}-{ver}' for lang, ver in VERSIONS}:
        raise ValueError('Expected an issued publication and six SDK versions')
    prompt = (f'q4t release record\npublication_revision: {revision}\n'
              'Copy the publication_revision value.\npublication_revision:')
    config = dict(base=base, key=key, certificate=str(certificate), model='qwen3.8-flash-next',
                  completion=dict(prompt=prompt, echo=True, temperature=0, seed=17,
                                  max_tokens=8, logprobs=1, stop=['\n']),
                  documents=[f'The q4t primary package has publication revision {revision}.',
                             f'The independent fallback package manifest is {info["fallback"]}.'], dimensions=32)
    out.mkdir(mode=0o700)
    public = {k:v for k,v in config.items() if k not in ('key','certificate','base')}
    (out/'scenario.json').write_text(json.dumps(public,indent=2)+'\n')
    traces = set()
    for label, sdk in sdks.items():
        python = label.startswith('python-')
        actor = Path(__file__).with_name('model_inference_sdk.' + ('py' if python else 'mjs'))
        args = [sdk, '-I', '-B', actor] if python else ['node', actor, sdk]
        process = subprocess.run(args, input=json.dumps(config).encode(), capture_output=True,
            timeout=110, env=dict(os.environ, NODE_EXTRA_CA_CERTS=str(certificate)))
        (out/(label+'.stderr')).write_text(process.stderr.decode(errors='replace').replace(key,'[redacted]'))
        process.check_returncode()
        record = json.loads(process.stdout)
        (out/(label+'.json')).write_text(json.dumps(record,ensure_ascii=False)+'\n')
        current = verify(record, public, label)
        if traces.intersection(current):
            raise ValueError('Two SDKs received the same HTTP request ID')
        traces.update(current)
    return traces
