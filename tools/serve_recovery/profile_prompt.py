"""Four real Prompt responses per capacity; this alone is not recovery evidence."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from tools.serve_realtime.ingress_fixture import NativeIngress, certificate
from tools.serve_recovery import profile_prompt_evidence as evidence
from tools.serve_recovery import profile_realtime as realtime
from tools.serve_service.package import read, require, save, sha

ROOT = Path(__file__).resolve().parents[2]
POSTS_PER_PROFILE = 4
SOURCES = ('tools/serve_recovery/profile_prompt.py',
    'tools/serve_recovery/profile_prompt_evidence.py',
    'tools/serve_realtime/prompt_sdk.mjs', 'tools/serve_realtime/prompt_definitions.json',
    'tools/serve_realtime/conversation_checks.mjs', 'tools/serve_realtime/ingress_fixture.py')


def prepare(source, output, public_port, sdk):
    source = Path(source).resolve(strict=True)
    require(source.is_relative_to(ROOT / '.q4t-work') and source.stat().st_size <= 2 << 20,
            'private actual Prompt continuation reference required')
    prior = read(source)
    from tools.serve_realtime import prompt_followup, prompt_acceptance
    prompt_followup.check_plan(prior)
    paths = [source, source.parent / 'result.json', source.parent / 'cleanup.json',
             source.parent.parent / 'lease/exit.json', source.parent / 'business.json',
             source.parent / 'remaining.sdk.json', source.parent / 'beta.sdk.json']
    result, cleanup, lease = (read(p) for p in paths[1:4])
    require(result['status'] == 'PASS' and result['default_unchanged'] is True and
        cleanup['remaining'] == [] and cleanup['model_exit'] == 0 and
        cleanup['ports_released'] is True and cleanup['lease_continuously_held'] is True and
        lease['status'] == 'COMPLETED' and lease['child_returncode'] == 0 and
        read(paths[4])['original_quality'] == 'FAIL' and
        read(paths[4])['original_stage_reclassified'] is False,
        'real continuation scope/cleanup or retained quality failure differs')
    prompt_followup.check_remaining(read(paths[5]), prior['foreign']['prompt'])
    prompt_acceptance.check_receipt(read(paths[6]), 'beta', prior['record'], prior['csv_sum'])
    files = {str(p): sha(p) for p in paths}
    for name, digest in prior['retained_files'].items():
        require(sha(name) == digest, 'original failed evidence changed')
        files[name] = digest
    # Retain the quality-failure reference, but each new profile must still
    # produce the correct four answers. No profile is inferred from this stage.
    rows = []
    for row in prior['csv_sources']:
        path = Path(row['path'])
        require(sha(path) == row['sha256'], 'actual CSV receipt changed')
        files[str(path)] = row['sha256']
        rows.append(dict(row, path=str(path.relative_to(ROOT))))
    csv = Path(prior['csv_path'])
    require(sha(csv) == prior['csv_sha256'], 'actual CSV content changed')
    files[str(csv)] = sha(csv)
    record = dict(csv=csv.read_text(), sha256=sha(csv), sum=prior['csv_sum'], receipts=rows)
    evidence.source(record)
    require(sha(prior['ingress']) == prior['ingress_sha256'], 'native ingress changed')
    files[prior['ingress']] = prior['ingress_sha256']
    tls = certificate(Path(output) / 'prompt-credentials')
    for key in ('certificate', 'key'): files[tls[key]] = sha(tls[key])
    return dict(format='q4t-profile-prompt-inputs-v1', files=files,
        sdk_directory=realtime.reuse_sdk(prior, sdk), ingress=prior['ingress'],
        tls=tls, public_port=public_port, csv_path=str(csv), source=record)


def run(stage):
    plan = stage.plan['prompt']; label = stage.profile_name + '-prompt'
    require(plan['format'] == 'q4t-profile-prompt-inputs-v1' and
        plan['sdk_directory'] == stage.plan['sdk']['sdk_directory'], 'Prompt SDK identity differs')
    for name, digest in plan['files'].items():
        require(sha(name) == digest, 'Prompt fixture drift: ' + name)
    client = realtime.encrypted.CLIENT
    policy = read(stage.out / 'client-policy.json')
    require(client in [r['id'] for r in policy['clients']], 'Prompt scoped client absent')
    key = hashlib.sha256(os.urandom(32)).hexdigest()

    def observe(suffix):
        status, raw = stage.http('/metrics', label=label + '-' + suffix)
        require(status == 200, 'Prompt metrics unavailable')
        return realtime.state(realtime.encrypted.foreground.base.lifecycle.parse_metrics(raw), client)

    before = observe('before')
    stage.budget('ingress_starts')
    edge = NativeIngress(stage.out / label, plan['ingress'], plan['public_port'],
        stage.plan['port'], stage.plan['operations_port'], policy['gateway_token'],
        {client: key}, plan['tls'], lambda: stage.model.poll() is None)
    guard = realtime.encrypted.foreground.base.lifecycle.guard
    stage.history[edge.process.pid] = guard.processes()[edge.process.pid]
    try:
        stage.budget('websocket_sessions')
        for _ in range(POSTS_PER_PROFILE): stage.budget('chats')
        for _ in range(4): stage.budget('prompt_registry_requests')
        args = dict(base_url=f"https://127.0.0.1:{plan['public_port']}/v1",
            api_key=key, ca_file=plan['tls']['certificate'], mode='qualification',
            csv_file=plan['csv_path'], csv_sha256=plan['source']['sha256'],
            csv_sum=plan['source']['sum'])
        env = {k: v for k, v in os.environ.items() if k not in ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        env['Q4T_NODE_SDK_DIR'] = plan['sdk_directory']
        run = subprocess.run([stage.plan['sdk']['node']['path'],
            str(ROOT / 'tools/serve_realtime/prompt_sdk.mjs')],
            input=json.dumps(args), capture_output=True, text=True, env=env, timeout=370)
        (stage.out / (label + '.sdk.json')).write_text(run.stdout)
        (stage.out / (label + '.stderr')).write_text(run.stderr)
        require(run.returncode == 0 and len(run.stdout.encode()) <= 4 << 20,
                'actual qualified Prompt SDK failed')
        session = json.loads(run.stdout)
        stage.same_model(); stage.drained(label + '-drain')
        deadline = time.monotonic() + 5
        after = observe('after-0'); count = 0
        while (after['sessions'] or after['host_bytes']) and time.monotonic() < deadline:
            time.sleep(.02); count += 1; after = observe('after-' + str(count))
        value = dict(source=plan['source'], session=session, before=before, after=after)
        evidence.business(value)
        save(stage.out / (label + '-business.json'), value)
        return value
    finally:
        edge.close()
        save(stage.out / (label + '-entry-cleanup.json'), dict(exit_code=edge.process.returncode,
            pid=edge.process.pid, port_released=True, guardian_qualified=False))
