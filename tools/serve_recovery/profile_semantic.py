"""Actual semantic VAD turns and health on each primary/light profile."""
import base64
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import time
import zlib

from tools.serve_realtime.ingress_fixture import NativeIngress
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_realtime as realtime
from tools.serve_service.package import asr_auxiliary_observed_limits, read, require, save, sha

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ('tools/serve_recovery/profile_semantic.py',
           'tools/serve_realtime/semantic_sdk.mjs')
POSTS_PER_PROFILE = 2


def compact(result, continuation):
    # Retrieved PCM is retained once. The complete untouched SDK event stream
    # stays beside the qualification receipt, including the retrieval event.
    value = copy.deepcopy(result)
    value['events'] = [e for e in value['events']
                       if e['type'] != 'conversation.item.retrieved']
    value.pop('feed', None)
    evidence.semantic_turn(value, continuation)
    content = value['retrieved']['item']['content'][0]
    raw = evidence.semantic_pcm(content['audio'])
    content['audio'] = dict(encoding='zlib-base64', pcm_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        data=base64.b64encode(zlib.compress(raw, 9)).decode())
    require(evidence.semantic_pcm(content['audio']) == raw,
            'semantic receipt compression changed original PCM')
    return value


def save_receipt(path, value):
    """Use compact JSON for audio receipts; keep the same durable 2 MiB gate."""
    path = Path(path)
    raw = evidence.ledger.canonical(value)+b'\n'
    require(len(raw) <= 2 << 20, 'semantic receipt exceeds metadata size limit')
    temporary = path.with_name(path.name+'.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    require(read(path) == value, 'semantic receipt did not round trip')


def health(stage, label):
    stage.budget('controls')
    conn = http.client.HTTPConnection('127.0.0.1', stage.plan['operations_port'], timeout=15)
    try:
        conn.request('GET', '/v1/config')
        response = conn.getresponse(); raw = response.read((1 << 20)+1)
        (stage.out/(label+'.wire')).write_bytes(raw)
        save(stage.out/(label+'.http.json'), dict(status=response.status))
        require(response.status == 200 and len(raw) <= 1 << 20,
                'semantic native operations health unavailable')
        return json.loads(raw)
    finally:
        conn.close()


def configuration(config):
    vad = asr_auxiliary_observed_limits(config, 'vad')
    require(type(vad['max_chunks']) is int and 0 < vad['max_chunks'] < 2**64,
            'native VAD chunk limit missing or invalid')
    return dict(asr_health=config['native_asr_health'],
        vad_health=config['native_vad_health'], asr_limits=config['native_asr_limits'],
        vad_limits=vad)


def run(stage):
    plan = stage.plan['realtime']; label = stage.profile_name+'-semantic'
    require(stage.plan.get('semantic_vad') is True and
        plan['sdk_directory'] == stage.plan['sdk']['sdk_directory'],
        'semantic qualification plan differs')
    for path, digest in plan['files'].items():
        require(sha(path) == digest, 'semantic fixture drift: '+path)
    client = realtime.encrypted.CLIENT
    policy = read(stage.out/'client-policy.json')
    key = hashlib.sha256(os.urandom(32)).hexdigest()
    def observe(suffix):
        status, raw = stage.http('/metrics', label=label+'-'+suffix)
        require(status == 200, 'semantic metrics unavailable')
        return realtime.state(realtime.encrypted.foreground.base.lifecycle.parse_metrics(raw), client)
    before = observe('before')
    stage.budget('ingress_starts')
    edge = NativeIngress(stage.out/label, plan['ingress'], plan['public_port'],
        stage.plan['port'], stage.plan['operations_port'], policy['gateway_token'],
        {client:key}, plan['tls'], lambda:stage.model.poll() is None)
    guard = realtime.encrypted.foreground.base.lifecycle.guard
    stage.history[edge.process.pid] = guard.processes()[edge.process.pid]
    try:
        turns = []
        for name, split in (('complete', 0), ('continuation', 36000)):
            stage.budget('websocket_sessions'); stage.budget('chats')
            args = dict(base_url=f"https://127.0.0.1:{plan['public_port']}/v1",
                api_key=key, ca_file=plan['tls']['certificate'], file=plan['clip']['path'],
                split_samples=split, settings=evidence.semantic_settings())
            env = {k:v for k,v in os.environ.items()
                   if k not in ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
            env['Q4T_NODE_SDK_DIR'] = plan['sdk_directory']
            process = subprocess.run([stage.plan['sdk']['node']['path'],
                str(ROOT/'tools/serve_realtime/semantic_sdk.mjs')],
                input=json.dumps(args), capture_output=True, text=True, env=env, timeout=135)
            (stage.out/(label+'-'+name+'.sdk.json')).write_text(process.stdout)
            (stage.out/(label+'-'+name+'.stderr')).write_text(process.stderr)
            require(process.returncode == 0 and len(process.stdout.encode()) <= 6 << 20,
                    'actual semantic SDK failed: '+name)
            turns.append(compact(json.loads(process.stdout), bool(split)))
            stage.same_model(); stage.drained(label+'-'+name+'-drain')
        deadline = time.monotonic()+5
        after = observe('after-0'); observation = 0
        while (after['sessions'] or after['host_bytes']) and time.monotonic() < deadline:
            time.sleep(.02); observation += 1
            after = observe('after-'+str(observation))
        config = health(stage, label+'-health')
        value = dict(input_sha256=plan['clip']['sha256'], settings=evidence.semantic_settings(),
            turns=turns, before=before, after=after,
            **configuration(config))
        evidence.semantic_business(value, stage.plan['identity'])
        save(stage.out/(label+'-business.json'), value)
        return value
    finally:
        edge.close()
        save(stage.out/(label+'-entry-cleanup.json'), dict(
            exit_code=edge.process.returncode, pid=edge.process.pid,
            port_released=True, guardian_qualified=False))
