"""Owned native TLS/SDK transcription for each primary/light qualification.

The admission peer here is a PID-bound fixture, not Guardian qualification.
Model lifecycle stays with the enclosing one-lease driver; no request replay.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess

from tools.serve_realtime.ingress_fixture import NativeIngress, certificate
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_service.package import read, require, save, sha

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ('tools/serve_recovery/profile_realtime.py',
    'tools/serve_realtime/ingress_fixture.py',
    'tools/serve_realtime/history_sdk.mjs')
POSTS_PER_PROFILE = 3  # Per-item model work, not HTTP POST or WSS handshakes.
MAX_RECEIPT_BYTES = 16 << 20


def reference_cleanup(prior, result, accepted):
    ports = [prior[k] for k in ('port', 'operations_port', 'public_port')]
    require(prior['schema'] == 'q4t-realtime-history-business-v1' and
        result['status'] == 'PASS' and
        accepted['status'] == 'PASS_REALTIME_HISTORY_RETRIEVAL_BUSINESS_RECONCILED' and
        all(accepted[k] is True for k in
            ('owned_processes_gone', 'gpu_lock_released', 'default_unchanged')) and
        type(accepted.get('ports_released')) is list and
        all(type(p) is int for p in accepted['ports_released']) and
        accepted['ports_released'] == ports,
        'prior processed-waveform/history reference was not qualified')


def prepare(source, output, public_port):
    source = Path(source).resolve(strict=True)
    require(source.is_relative_to(ROOT/'.q4t-work') and source.stat().st_size <= 4 << 20,
            'bounded private qualified Realtime plan required')
    prior = read(source)
    result = source.parent/'result.json'
    reconciled = source.parent.parent/'reconciled.json'
    accepted = read(reconciled)
    reference_cleanup(prior, read(result), accepted)
    clip = prior['clips']['zh']
    require(clip['sha256'] == evidence.REALTIME_INPUT_SHA and
        clip['source_sha256'] == evidence.ASR_FILE_SHA and clip['bytes'] == 201790 and
        prior['waveforms']['manual']['sha256'] == evidence.REALTIME_WAVEFORM_SHA and
        prior['reference']['base_input_tokens'] == 70 and
        prior['reference']['history_tokens'] == 11,
        'fixed Realtime input/independent reference differs')
    files = {**prior['sdk_files'], str(source):sha(source), str(result):sha(result),
             str(reconciled):sha(reconciled), clip['path']:clip['sha256'],
             clip['source']:clip['source_sha256'], prior['ingress']:prior['ingress_sha256']}
    for path, digest in files.items():
        require(sha(path) == digest, 'Realtime fixture identity differs: '+path)
    tls = certificate(Path(output)/'realtime-credentials')
    for key in ('certificate', 'key'): files[tls[key]] = sha(tls[key])
    return dict(format='q4t-profile-realtime-inputs-v1',
        source_plan=str(source), files=files, clip=clip,
        sdk_directory=prior['sdk_directory'], ingress=prior['ingress'],
        tls=tls, public_port=public_port, settings=prior['settings']['manual'])


def state(values, client):
    keys = dict(admitted='q4t_client_rate_admitted_total{client="'+client+'"}',
        settled='q4t_client_rate_settled_tokens_total{client="'+client+'"}',
        reserved='q4t_client_rate_reserved_tokens{client="'+client+'"}',
        active='q4t_active_chats', sessions='q4t_realtime_sessions',
        host_bytes='q4t_realtime_host_reserved_bytes', free_slots='q4t_seq_slots_free')
    result = {}
    for name, key in keys.items():
        value = values.get(key)
        require(type(value) in (int, float) and math.isfinite(value) and
                value >= 0 and value == int(value),
                'missing/noninteger Realtime metric: '+key)
        result[name] = int(value)
    return result


def compact(result):
    require(type(result) is dict and all(k in result for k in
        ('status', 'connections', 'close_code', 'automatic_replay', 'turns', 'checks')),
        'incomplete actual Realtime SDK receipt')
    return {k:result[k] for k in
        ('status', 'connections', 'close_code', 'automatic_replay', 'turns', 'checks')}


def run(stage):
    plan = stage.plan['realtime']; label = stage.profile_name+'-realtime'
    require(plan['format'] == 'q4t-profile-realtime-inputs-v1', 'Realtime input format')
    for path, digest in plan['files'].items():
        require(sha(path) == digest, 'Realtime fixture drift: '+path)
    client = encrypted.CLIENT
    policy = read(stage.out/'client-policy.json')
    require(client in [row['id'] for row in policy['clients']], 'scoped Realtime client absent')
    key = hashlib.sha256(os.urandom(32)).hexdigest()
    def observe(suffix):
        status, raw = stage.http('/metrics', label=label+'-'+suffix)
        require(status == 200, 'Realtime metrics unavailable')
        return state(encrypted.foreground.base.lifecycle.parse_metrics(raw), client)
    before = observe('before')
    stage.budget('ingress_starts')
    edge = NativeIngress(stage.out/label, plan['ingress'], plan['public_port'],
        stage.plan['port'], stage.plan['operations_port'], policy['gateway_token'],
        {client:key}, plan['tls'], lambda:stage.model.poll() is None)
    guard = encrypted.foreground.base.lifecycle.guard
    stage.history[edge.process.pid] = guard.processes()[edge.process.pid]
    try:
        stage.budget('websocket_sessions')
        for _ in range(POSTS_PER_PROFILE): stage.budget('chats')
        args = dict(base_url=f"https://127.0.0.1:{plan['public_port']}/v1", api_key=key,
            ca_file=plan['tls']['certificate'], file=plan['clip']['path'],
            mode='manual', settings=plan['settings'])
        env = {k:v for k,v in os.environ.items() if k not in
               ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        env['Q4T_NODE_SDK_DIR'] = plan['sdk_directory']
        # The qualified SDK enforces its own 8 MiB events/120 s lifetime bound.
        run = subprocess.run([stage.plan['sdk']['node']['path'],
            str(ROOT/'tools/serve_realtime/history_sdk.mjs')], input=json.dumps(args),
            capture_output=True, text=True, env=env, timeout=135)
        (stage.out/(label+'.sdk.json')).write_text(run.stdout)
        (stage.out/(label+'.stderr')).write_text(run.stderr)
        require(run.returncode == 0 and len(run.stdout.encode()) <= MAX_RECEIPT_BYTES,
                'bounded actual Realtime SDK failed')
        result = json.loads(run.stdout)
        stage.same_model();stage.drained(label+'-drain')
        # WSS close has been acknowledged; the owner has a short bounded join.
        import time
        deadline = time.monotonic()+5
        observation = 0
        after = observe('after-0')
        while (after['sessions'] or after['host_bytes']) and time.monotonic() < deadline:
            time.sleep(.02); observation += 1
            after = observe('after-'+str(observation))
        value = dict(input_sha256=plan['clip']['sha256'], session=compact(result),
                     before=before, after=after)
        evidence.realtime_business(value)
        save(stage.out/(label+'-business.json'), value)
        return value
    finally:
        edge.close()
        save(stage.out/(label+'-entry-cleanup.json'), dict(
            exit_code=edge.process.returncode, pid=edge.process.pid,
            port_released=True, guardian_qualified=False))
