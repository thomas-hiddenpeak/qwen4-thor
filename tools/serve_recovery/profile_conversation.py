"""Actual native Realtime Chat on each qualified capacity, without ASR binding.

The owned TLS admission fixture is not Guardian recovery evidence. Each model
turn uses normal client admission; a failed/ambiguous call is never replayed.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from tools.serve_realtime.ingress_fixture import NativeIngress, certificate
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_realtime as realtime
from tools.serve_service.package import read, require, save, sha

ROOT = Path(__file__).resolve().parents[2]
POSTS_PER_PROFILE = 2
SOURCES = ('tools/serve_recovery/profile_conversation.py',
    'tools/serve_realtime/conversation_sdk.mjs',
    'tools/serve_realtime/conversation_checks.mjs',
    'tools/serve_realtime/ingress_fixture.py')


def prepare(source, output, public_port, sdk):
    source = Path(source).resolve(strict=True)
    require(source.is_relative_to(ROOT/'.q4t-work') and
            source.stat().st_size <= 2 << 20,
            'private qualified conversation reference required')
    prior = read(source)
    result_path = source.parent/'result.json'
    cleanup_path = source.parent/'cleanup.json'
    lease_path = source.parent.parent/'lease/exit.json'
    receipt_path = source.parent/'main.sdk.json'
    result, cleanup, lease = (read(p) for p in
                              (result_path, cleanup_path, lease_path))
    require(prior['schema'] == 'q4t-realtime-conversation-business-v1' and
        result['status'] == 'PASS' and result['default_unchanged'] is True and
        cleanup['remaining'] == [] and cleanup['model_exit'] == 0 and
        cleanup['ports_released'] is True and
        cleanup['lease_continuously_held'] is True and
        lease['status'] == 'COMPLETED' and lease['child_returncode'] == 0,
        'original real conversation or cleanup was not accepted')
    from tools.serve_realtime.conversation_acceptance import check_receipt
    check_receipt(read(receipt_path), 'main', prior['record'])
    sdk_directory = realtime.reuse_sdk(prior, sdk)
    record = ROOT/'docs/STATUS.md'
    require(record.stat().st_size <= 1 << 20, 'bounded current project record')
    version = re.search(r'默认\s*rev([0-9]+)', record.read_text())
    require(version is not None, 'actual current release record missing')
    files = {str(p):sha(p) for p in
             (source, result_path, cleanup_path, lease_path, receipt_path, record)}
    require(sha(prior['ingress']) == prior['ingress_sha256'],
            'qualified native entry identity changed')
    files[prior['ingress']] = prior['ingress_sha256']
    tls = certificate(Path(output)/'conversation-credentials')
    for key in ('certificate', 'key'): files[tls[key]] = sha(tls[key])
    return dict(format='q4t-profile-conversation-inputs-v1', files=files,
        sdk_directory=sdk_directory, ingress=prior['ingress'],
        tls=tls, public_port=public_port,
        source=dict(path='docs/STATUS.md', sha256=sha(record),
                    version='rev'+version[1]))


def run(stage):
    plan = stage.plan['conversation']; label = stage.profile_name+'-conversation'
    require(plan['format'] == 'q4t-profile-conversation-inputs-v1' and
        plan['sdk_directory'] == stage.plan['sdk']['sdk_directory'],
        'conversation qualification SDK identity differs')
    for path, digest in plan['files'].items():
        require(sha(path) == digest, 'conversation fixture drift: '+path)
    client = realtime.encrypted.CLIENT
    policy = read(stage.out/'client-policy.json')
    require(client in [row['id'] for row in policy['clients']],
            'scoped conversation client absent')
    key = hashlib.sha256(os.urandom(32)).hexdigest()

    def observe(suffix):
        status, raw = stage.http('/metrics', label=label+'-'+suffix)
        require(status == 200, 'conversation metrics unavailable')
        return realtime.state(
            realtime.encrypted.foreground.base.lifecycle.parse_metrics(raw), client)

    before = observe('before')
    stage.budget('ingress_starts')
    edge = NativeIngress(stage.out/label, plan['ingress'], plan['public_port'],
        stage.plan['port'], stage.plan['operations_port'], policy['gateway_token'],
        {client:key}, plan['tls'], lambda:stage.model.poll() is None)
    guard = realtime.encrypted.foreground.base.lifecycle.guard
    stage.history[edge.process.pid] = guard.processes()[edge.process.pid]
    try:
        stage.budget('websocket_sessions')
        for _ in range(POSTS_PER_PROFILE): stage.budget('chats')
        args = dict(base_url=f"https://127.0.0.1:{plan['public_port']}/v1",
            api_key=key, ca_file=plan['tls']['certificate'], mode='qualification',
            record_file=str(ROOT/plan['source']['path']),
            record_sha256=plan['source']['sha256'],
            expected_version=plan['source']['version'])
        env = {k:v for k,v in os.environ.items()
               if k not in ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        env['Q4T_NODE_SDK_DIR'] = plan['sdk_directory']
        process = subprocess.run([stage.plan['sdk']['node']['path'],
            str(ROOT/'tools/serve_realtime/conversation_sdk.mjs')],
            input=json.dumps(args), capture_output=True, text=True, env=env,
            timeout=255)
        (stage.out/(label+'.sdk.json')).write_text(process.stdout)
        (stage.out/(label+'.stderr')).write_text(process.stderr)
        require(process.returncode == 0 and
                len(process.stdout.encode()) <= 4 << 20,
                'actual conversation SDK failed')
        session = json.loads(process.stdout)
        stage.same_model(); stage.drained(label+'-drain')
        deadline = time.monotonic()+5
        after = observe('after-0'); observation = 0
        while (after['sessions'] or after['host_bytes']) and time.monotonic() < deadline:
            time.sleep(.02); observation += 1
            after = observe('after-'+str(observation))
        value = dict(source=plan['source'], session=session,
                     before=before, after=after)
        evidence.conversation_business(value)
        save(stage.out/(label+'-business.json'), value)
        return value
    finally:
        edge.close()
        save(stage.out/(label+'-entry-cleanup.json'), dict(
            exit_code=edge.process.returncode, pid=edge.process.pid,
            port_released=True, guardian_qualified=False))
