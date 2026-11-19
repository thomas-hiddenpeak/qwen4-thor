"""Owned official WSS speech on each capacity; no extra model or HTTP loop."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from tools.serve_realtime.ingress_fixture import NativeIngress, certificate
from tools.serve_recovery import profile_realtime, profile_speech_evidence as evidence
from tools.serve_recovery import profile_refusal_reuse as voice
from tools.serve_service.package import read, require, save, sha

ROOT = Path(__file__).resolve().parents[2]


def prepare(previous, output, public_port, sdk):
    source = Path(previous).resolve(strict=True)
    plan = read(source)
    require(plan['schema'] == 'q4t-realtime-speech-content-v1' and
        read(source.parent / 'result.json')['status'] == 'PASS' and
        read(source.parent.parent / 'lease/exit.json')['status'] == 'COMPLETED',
        'actual original speech content required')
    tls = certificate(Path(output) / 'speech-credentials')
    profile_realtime.reuse_sdk(plan, sdk)
    files = {str(p): sha(p) for p in (source, source.parent / 'result.json',
        source.parent.parent / 'lease/exit.json', Path(plan['ingress']),
        Path(tls['certificate']), Path(tls['key']))}
    require(sha(plan['ingress']) == plan['ingress_sha256'], 'qualified ingress drift')
    return dict(sdk_directory=sdk['sdk_directory'], ingress=plan['ingress'],
                public_port=public_port, tls=tls, files=files)


def run(stage):
    plan = stage.plan['speech']; label = stage.profile_name + '-speech'
    for path, digest in plan['files'].items():
        require(sha(path) == digest, 'speech fixture drift')
    policy = read(stage.out / 'client-policy.json')
    clients = {profile_realtime.encrypted.CLIENT: hashlib.sha256(os.urandom(32)).hexdigest(),
               'audio': hashlib.sha256(os.urandom(32)).hexdigest()}
    stage.budget('ingress_starts')
    edge = NativeIngress(stage.out / label, plan['ingress'], plan['public_port'],
        stage.plan['port'], stage.plan['operations_port'], policy['gateway_token'],
        clients, plan['tls'], lambda: stage.model.poll() is None)
    guard = profile_realtime.encrypted.foreground.base.lifecycle.guard
    stage.history[edge.process.pid] = guard.processes()[edge.process.pid]
    witnesses = {}
    try:
        for mode in evidence.CASES:
            client = 'audio' if mode == 'custom' else profile_realtime.encrypted.CLIENT
            case_label = label + '-' + mode

            def observe(suffix):
                status, raw = stage.http('/metrics', label=case_label + '-' + suffix)
                require(status == 200, 'speech metrics unavailable')
                return profile_realtime.state(
                    profile_realtime.encrypted.foreground.base.lifecycle.parse_metrics(raw), client)

            before = observe('before')
            stage.budget('websocket_sessions')
            for _ in range(2 if mode == 'foreign' else 1): stage.budget('chats')
            args = dict(base_url=f"https://127.0.0.1:{plan['public_port']}/v1",
                api_key=clients[client], ca_file=plan['tls']['certificate'], mode='profile_' + mode)
            env = {k: v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD')}
            env['Q4T_NODE_SDK_DIR'] = plan['sdk_directory']
            process = subprocess.run([stage.plan['sdk']['node']['path'],
                str(ROOT / 'tools/serve_realtime/speech_sdk.mjs')],
                input=json.dumps(args), capture_output=True, text=True, env=env, timeout=180)
            (stage.out / (case_label + '.sdk.json')).write_text(process.stdout)
            (stage.out / (case_label + '.stderr')).write_text(process.stderr)
            require(process.returncode == 0 and len(process.stdout.encode()) <= 2 << 20,
                    'actual speech SDK failed')
            session = json.loads(process.stdout)
            stage.same_model(); stage.drained(case_label + '-drain')
            end = time.monotonic() + 5; index = 0; after = observe('after-0')
            while (after['sessions'] or after['host_bytes']) and time.monotonic() < end:
                time.sleep(.02); index += 1; after = observe('after-' + str(index))
            witnesses[mode] = dict(session=session, before=before, after=after)
        value = dict(format=evidence.FORMAT, snapshot_sha256=voice.VOICE_SNAPSHOT,
                     sessions=witnesses)
        # Keep every original response even when exact quality/content fails.
        save(stage.out / (label + '-observed.json'), value)
        evidence.business(value)
        return value
    finally:
        edge.close()
        save(stage.out / (label + '-entry-cleanup.json'), dict(
            exit_code=edge.process.returncode, pid=edge.process.pid, port_released=True))
