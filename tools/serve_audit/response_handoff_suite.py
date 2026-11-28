"""Exchange six project receipts across official SDKs on a private TLS endpoint."""
import json
import os
from pathlib import Path
import subprocess

from .response_handoff_receipt import verify
from .sdk_business_evidence import CLIENTS


def save(path, record):
    with path.open('x') as output:
        json.dump(record, output, indent=2, ensure_ascii=False)
        output.write('\n')


def message(role, text):
    return dict(type='message', role=role, content=[dict(type='input_text', text=text)])


def run(out, base, keys, cert, sdk_paths, documents):
    def invoke(label, name, config):
        python = label.startswith('python-')
        actor = Path(__file__).with_name('response_handoff.py' if python else 'response_handoff.mjs')
        sdk = sdk_paths[label]
        args = [sdk, '-I', '-B', actor] if python else ['node', actor, sdk]
        capture = out / (name + '-wire.jsonl')
        capture.touch(mode=0o600, exist_ok=False)
        config.update(base=base, key=keys['alpha'], peer=keys['beta'], ca=str(cert), capture=str(capture))
        observation = subprocess.run(args, input=json.dumps(config).encode(), capture_output=True,
                                     timeout=75, env=dict(os.environ, NODE_EXTRA_CA_CERTS=str(cert)))
        diagnostic = observation.stderr.decode(errors='replace')
        for secret in keys.values():
            diagnostic = diagnostic.replace(secret, '<redacted>')
        (out / (name + '.log')).write_text(diagnostic)
        if observation.returncode:
            raise RuntimeError(f'{name} failed, see its actor log')
        record = json.loads(observation.stdout)
        assert record['sdk'] == label and record['beta'] is config['beta']
        assert record['requests'] == [json.loads(line) for line in capture.read_bytes().splitlines()]
        save(out / (name + '.json'), record)
        return record

    traces, response_ids = set(), set()
    scenarios = []
    for i, (label, document) in enumerate(zip(CLIENTS, documents, strict=True)):
        reader = CLIENTS[(i + 1) % len(CLIENTS)]
        path = Path(document)
        raw = path.read_bytes()
        original = json.loads(raw)
        assert original['status'] == 'PASS' and original['desired'] == 'STOPPED'
        options = dict(model='qwen3.8-flash-next', store=True, temperature=0,
                       reasoning=dict(effort='none'), max_output_tokens=48)
        request = dict(options, metadata=dict(writer=label), input=[
            message('developer', 'For each question reply with only the requested integer from the project receipt.'),
            message('user', 'What is the publication_revision in this q4t publication receipt?\n' + raw.decode())])
        followup = dict(options, metadata=dict(reader=reader),
                        input='From that same receipt, what is the version_revision? Reply only with that integer.')
        writer = invoke(label, f'{i}-write', dict(action='write', request=request,
                        beta=label in ('python-3.55.1', 'node-7.47.0')))
        result = invoke(reader, f'{i}-read', dict(action='read', id=writer['created']['id'], followup=followup,
                        beta=reader in ('python-3.42.1', 'python-3.55.1', 'node-7.47.0')))
        group = verify(writer, result, request, followup, raw)
        assert traces.isdisjoint(group)
        traces.update(group)
        for ident in (writer['created']['id'], result['follow']['id']):
            assert ident not in response_ids
            response_ids.add(ident)
        scenarios.append(dict(writer=writer, reader=result, request=request, followup=followup,
                              source=path.parent.name))
    assert len(traces) == 60 and len(response_ids) == 12
    save(out / 'scenarios.json', scenarios)
    return traces
