"""Actual managed Realtime work, explicit reconnection and bounded receipts.

This acceptance actor never launches a model or substitutes for Guardian. It
uses the enclosing production service, immutable qualified inputs and one
client process per connection. CPU witnesses cannot qualify a payload.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import time

from tools.serve_recovery import profile_evidence as evidence
from tools.serve_service.package import read, require, same_json, save, sha

ROOT = Path(__file__).resolve().parents[2]
MAX_RECEIPT = 4 << 20
METRICS = dict(
    admitted='q4t_client_rate_admitted_total{client="alpha"}',
    settled='q4t_client_rate_settled_tokens_total{client="alpha"}',
    reserved='q4t_client_rate_reserved_tokens{client="alpha"}',
    active='q4t_active_chats', sessions='q4t_realtime_sessions',
    host_bytes='q4t_realtime_host_reserved_bytes', free_slots='q4t_seq_slots_free',
    total='q4t_requests_total', success='q4t_requests_success_total',
    aborted='q4t_requests_aborted_total', errors='q4t_requests_error_total')


def phases(plan):
    return {'tool_retained': [('primary', 'complete')],
            'tls': [('tls-before', 'hold_stream'), ('tls-after', 'complete')],
            'tool_version': [('primary', 'hold_complete'),
                             ('fallback', 'complete')]}[plan['scenario']]


def settings(stream=False):
    transcription = dict(model='qwen3-asr-0.6b')
    if stream:
        transcription['delay'] = 'low'
    return dict(type='transcription',
        include=['item.input_audio_transcription.logprobs'],
        audio=dict(input=dict(format=dict(type='audio/pcmu') if stream else
            dict(type='audio/pcm', rate=24000), transcription=transcription,
            turn_detection=None,
            noise_reduction=dict(type='near_field') if stream else None)))


def prepare(path, manifest, qualified, package):
    require(path is not None, 'declared Realtime requires managed verification')
    path = Path(path).resolve(strict=True)
    require(path.is_relative_to(ROOT/'.q4t-work'), 'private qualified plan required')
    prior = read(path)
    registry_path = path.parent/'qualified/registry.json'
    registry = read(registry_path)
    acceptance_path = path.parent/'acceptance.json'
    accepted = read(acceptance_path)
    require(accepted['status'] == 'ACCEPT_PRIMARY_LIGHT_QUALIFICATION' and
        accepted['registry_sha256'] == sha(registry_path) and
        same_json(registry, manifest['recovery_profiles']) and
        same_json(read(path.parent/'qualified/primary.json'), qualified) and
        same_json(read(path.parent/'qualified/light.json'),
                  read(Path(package)/'profile-light.json')),
        'managed Realtime requires exact accepted primary/light identities')
    original_path = Path(prior['realtime']['source_plan'])
    original = read(original_path)
    require(original['schema'] == 'q4t-realtime-history-business-v1' and
        original['settings'] == dict(manual=settings(), near_stream=settings(True)) and
        original['clips']['zh']['sha256'] == evidence.REALTIME_INPUT_SHA and
        original['clips']['mulaw']['sha256'] ==
            '42d2b524d9f7d1b52640c00b99f55360556a128903e860bc47c86fc064adeca7' and
        original['reference']['base_input_tokens'] == 70 and
        original['reference']['history_tokens'] == 11 and
        original['waveforms']['manual']['sha256'] == evidence.REALTIME_WAVEFORM_SHA,
        'fixed managed Realtime reference differs')
    files = {str(p): sha(p) for p in (path, registry_path, acceptance_path,
        path.parent/'qualified/primary.json', path.parent/'qualified/light.json',
        original_path)}
    files.update(prior['realtime']['files'])
    clips = {name: original['clips'][name] for name in ('zh', 'mulaw')}
    for clip in clips.values():
        files[clip['path']] = clip['sha256']
        require(Path(clip['path']).stat().st_size == clip['bytes'], 'input size drift')
    for name, digest in files.items():
        require(sha(name) == digest, 'managed Realtime reference drift: '+name)
    return dict(format='q4t-managed-realtime-v1', clips=clips,
        settings=original['settings'], files=files), prior['sdk']


def state(raw):
    require(type(raw) is str and len(raw.encode()) <= 1 << 20, 'metrics bound')
    result = {}
    for name, key in METRICS.items():
        rows = [line for line in raw.splitlines()
                if line.split(' ', 1)[0] == key]
        require(len(rows) == 1 and re.fullmatch(re.escape(key)+r' [0-9]+', rows[0]),
                'missing/invalid exact Realtime metric: '+key)
        value = int(rows[0].split(' ')[-1])
        require(value < 2**64, 'Realtime metric overflow')
        result[name] = value
    return result


def state_shape(value):
    require(type(value) is dict and value.keys() == METRICS.keys() and all(
        type(v) is int and 0 <= v < 2**64 for v in value.values()),
        'invalid managed Realtime state')


def clean(value):
    state_shape(value)
    return value['free_slots'] == 1 and all(value[k] == 0 for k in
        ('reserved', 'active', 'sessions', 'host_bytes'))


def terminal(value):
    turn = value['turn']
    require(type(turn) is dict and value['partial'] is None, 'complete turn absent')
    committed, done, item = turn['committed'], turn['terminal'], turn['retrieved']['item']
    identifier = turn['item_id']
    require(type(identifier) is str and identifier and
        committed['type'] == 'input_audio_buffer.committed' and
        done['type'] == 'conversation.item.input_audio_transcription.completed' and
        turn['retrieved']['type'] == 'conversation.item.retrieved' and
        committed['item_id'] == done['item_id'] == item['id'] == identifier and
        committed['previous_item_id'] is None and
        turn['delta'] == done['transcript'] == evidence.ASR_TEXT and
        item['object'] == 'realtime.item' and item['type'] == 'message' and
        item['role'] == 'user' and item['status'] == 'completed' and
        type(item['content']) is list and len(item['content']) == 1,
        'managed actual transcript or fresh history differs')
    content = item['content'][0]
    require(content.keys() == {'type', 'transcript', 'audio'} and
        content['type'] == 'input_audio' and content['transcript'] == evidence.ASR_TEXT and
        type(content['audio']) is str and len(content['audio']) < 1 << 20,
        'managed retrieved content differs')
    raw = base64.b64decode(content['audio'], validate=True)
    require(base64.b64encode(raw).decode() == content['audio'] and
        len(raw) == 201788 and hashlib.sha256(raw).hexdigest() == evidence.REALTIME_WAVEFORM_SHA,
        'managed retrieved waveform differs')
    usage = done['usage']
    require(usage['type'] == 'tokens' and all(type(usage[k]) is int for k in
        ('input_tokens', 'output_tokens', 'total_tokens')) and
        [usage[k] for k in ('input_tokens', 'output_tokens', 'total_tokens')] == [70, 11, 81],
        'managed actual token usage differs')
    return identifier, 81


def check_session(value, mode, transport):
    require(type(value) is dict and value['phase'] == 'result' and
        value['status'] == 'PASS' and value['mode'] == mode and
        value['transport'] == transport and type(value['connections']) is int and
        value['connections'] == 1 and value['automatic_replay'] is False and
        type(value['close_code']) is int and
        value['close_code'] in ((1000,) if mode == 'complete' else (1001, 1006, 1011, 1012)),
        'managed connection failed, changed transport or retried')
    events = value['events']
    require(type(events) is list and 0 < len(events) <= 1024 and
        type(value['event_bytes']) is int and 0 < value['event_bytes'] <= MAX_RECEIPT and
        type(value['sent_events']) is int and 0 < value['sent_events'] <= 256 and
        not any(v.get('type') == 'error' for v in events), 'managed event bounds/errors')
    require(all(type(value[k]) is int and value[k] > 0 for k in
        ('sent_audio_bytes', 'total_audio_bytes')) and
        type(value['transport_errors']) is list and
        len(value['transport_errors']) <= 4 and
        (not value['transport_errors'] or mode != 'complete'),
        'managed wire counts or unexpected transport failure')
    require(sum(v.get('type') == 'session.created' for v in events) == 1 and
        sum(v.get('type') == 'session.updated' for v in events) == 1,
        'managed session restarted')
    expected = settings(mode == 'hold_stream')['audio']['input']
    expected['transcription'] = dict(language='', prompt='', languages=[], keywords=[],
                                      **expected['transcription'])
    updated = next(v for v in events if v['type'] == 'session.updated')
    require(same_json(updated['session']['audio']['input'], expected),
            'managed effective settings changed or transcript hinted')
    kinds = ('input_audio_buffer.committed',
             'conversation.item.input_audio_transcription.completed',
             'conversation.item.retrieved')
    if mode == 'hold_stream':
        require(value['turn'] is None and type(value['partial']) is dict and
            value['sent_audio_bytes'] == 24000 and value['total_audio_bytes'] == 33632 and
            not any(v['type'] in kinds for v in events),
            'withheld continuous input completed or was not withheld')
        identifier = value['partial']['item_id']
        deltas = [v for v in events if v['type'] ==
                  'conversation.item.input_audio_transcription.delta']
        text = ''.join(v['delta'] for v in deltas)
        require(deltas and all(v['item_id'] == identifier for v in deltas) and text and
            deltas[0]['delta'] == value['partial']['delta'] and
            evidence.ASR_TEXT.startswith(text), 'actual partial transcript differs')
        return identifier, None
    require(value['sent_audio_bytes'] == value['total_audio_bytes'] == 201790,
            'complete source input differs')
    identifier, tokens = terminal(value)
    require(all(sum(v['type'] == kind for v in events) == 1 for kind in kinds),
            'managed terminal replay/omission')
    require(all(value['turn'][key] in events for key in ('committed', 'terminal', 'retrieved')),
            'managed result not present in actual wire events')
    deltas = [v for v in events if v['type'] ==
              'conversation.item.input_audio_transcription.delta']
    require(deltas and all(v['item_id'] == identifier for v in deltas) and
        ''.join(v['delta'] for v in deltas) == evidence.ASR_TEXT,
        'managed actual delta differs')
    return identifier, tokens


def accounting(before, after, *, tokens=None, interrupted=False):
    state_shape(before); state_shape(after)
    require(after['admitted'] == before['admitted']+1 and
        after['total'] == before['total']+1 and after['errors'] == before['errors'] and
        after['success'] == before['success']+(0 if interrupted else 1) and
        after['aborted'] == before['aborted']+(1 if interrupted else 0),
        'managed model work missing, replayed or wrong termination')
    used = after['settled']-before['settled']
    require(used > 0 and (tokens is None or used == tokens),
            'actual model tokens were not settled')
    return used


def check(out, plan, identity, capabilities):
    enabled = evidence.realtime_enabled(identity, capabilities)
    require(('realtime' in plan) is enabled, 'managed Realtime capability omitted')
    if not enabled:
        return {}
    require(plan['realtime']['format'] == 'q4t-managed-realtime-v1', 'managed format')
    result = {}; identifiers = []; previous = None
    transport = 'openai-realtime-wss' if plan['scenario'] == 'tls' else 'ws-loopback'
    for label, mode in phases(plan):
        prefix = label+'-realtime'
        row = read(Path(out)/(prefix+'-receipt.json'))
        session = read(Path(out)/(prefix+'.sdk.json'))
        identifier, tokens = check_session(session, mode, transport)
        require(row['mode'] == mode and clean(row['before']), 'initial Realtime resource leak')
        if previous is not None and plan['scenario'] == 'tls':
            require(same_json(row['before'], previous),
                    'healthy model counters changed between recovery and explicit reconnect')
        for suffix in ('before', 'after') + (() if mode == 'complete' else ('held',)):
            if suffix == 'after' and mode == 'hold_complete':
                require(row['after'] is None, 'cannot subtract counters across model restart')
                continue
            observations = read(Path(out)/(prefix+'-'+suffix+'-metrics.json'))
            require(0 < len(observations) <= 128 and all(
                v['http_status'] == 200 for v in observations) and
                same_json(state(observations[-1]['body']), row[suffix]),
                'raw managed metrics do not match receipt')
        if mode != 'complete':
            held = row['held']; state_shape(held)
            ready = read(Path(out)/(prefix+'-ready.json'))
            require(ready['phase'] == 'ready' and ready['mode'] == mode and
                ready['item_id'] == identifier and ready['transport'] == transport and
                ready['connections'] == 1 and
                ready['delta'] == (session['partial']['delta'] if mode == 'hold_stream'
                                   else session['turn']['delta']) and
                ready['sent_audio_bytes'] == session['sent_audio_bytes'] and
                ready['total_audio_bytes'] == session['total_audio_bytes'] and
                held['sessions'] == 1 and held['host_bytes'] > 0,
                'fault did not interrupt the held actual connection')
        if mode == 'hold_complete':
            require(held['active'] == held['reserved'] == 0,
                    'payload fault must follow completed settled task')
            used = accounting(row['before'], held, tokens=tokens)
        else:
            require(clean(row['after']), 'managed old session/resource reservation leaked')
            if mode == 'hold_stream':
                require(held['active'] == 1 and held['reserved'] > 0,
                        'gateway fault did not interrupt unfinished model work')
            used = accounting(row['before'], row['after'], tokens=tokens,
                              interrupted=mode == 'hold_stream')
        identifiers.append(identifier)
        previous = row['after']
        result[prefix] = dict(mode=mode, transport=transport,
            close_code=session['close_code'], actual_tokens=used,
            completed=mode != 'hold_stream', automatic_replay=False)
    require(len(set(identifiers)) == len(identifiers), 'new connection reused old item identity')
    counts = read(Path(out)/'counts.json')['counts']
    expected = len(result)*(2 if plan.get('semantic_vad') else 1)
    require(counts['websocket_sessions'] == plan['limits']['websocket_sessions'] == expected,
            'managed connection count differs')
    return result


class Actor:
    def __init__(self, stage):
        self.stage = stage
        self.process = self.stderr = None
        self.buffer = bytearray()
        self.total_bytes = 0
        self.attempted = []
        self.current = None

    def observe(self, suffix, predicate=lambda _: True):
        from tools.serve_ops import transport
        label = self.current['prefix']+'-'+suffix
        path = self.stage.output/(label+'-metrics.json')
        rows = []; end = time.monotonic()+5
        while time.monotonic() < end and len(rows) < 128:
            self.stage.budget.reserve('controls', label)
            response = transport.get(self.stage.policy['upstream_operations_port'],
                                     '/metrics', timeout=1)
            rows.append(response); save(path, rows)
            require(response['http_status'] == 200, 'managed metrics unavailable')
            values = state(response['body'])
            if predicate(values):
                return values
            time.sleep(.05)
        raise TimeoutError('managed Realtime resource observation: '+suffix)

    def line(self, seconds):
        end = time.monotonic()+seconds
        while b'\n' not in self.buffer:
            remaining = end-time.monotonic()
            require(remaining > 0, 'managed client receipt deadline')
            ready = select.select([self.process.stdout], [], [], remaining)[0]
            require(ready, 'managed client receipt timeout')
            data = os.read(self.process.stdout.fileno(), 65536)
            require(data, 'managed client closed before receipt')
            self.total_bytes += len(data)
            require(self.total_bytes <= MAX_RECEIPT, 'managed client output bound')
            self.buffer.extend(data)
        raw, _, rest = self.buffer.partition(b'\n')
        self.buffer = bytearray(rest)
        return json.loads(raw)

    def start(self, label):
        expected = phases(self.stage.plan)
        require(self.current is None and len(self.attempted) < len(expected) and
            label == expected[len(self.attempted)][0], 'managed connection replay or order')
        mode = expected[len(self.attempted)][1]
        self.attempted.append(label)
        prefix = label+'-realtime'
        self.current = dict(prefix=prefix, mode=mode)
        self.current['before'] = self.observe('before', clean)
        self.stage.budget.reserve('websocket_sessions', prefix)
        self.stage.budget.reserve('chats', prefix)
        fixed = self.stage.plan['realtime']
        clip = fixed['clips']['mulaw' if mode == 'hold_stream' else 'zh']
        require(sha(clip['path']) == clip['sha256'], 'managed input changed')
        tls = self.stage.plan['scenario'] == 'tls'
        args = dict(base_url=f"{'https' if tls else 'http'}://127.0.0.1:{self.stage.port}/v1",
            api_key=self.stage.keys['alpha'], file=clip['path'], mode=mode,
            ca_file=self.stage.plan['tls_inputs']['ca']['path'] if tls else None,
            settings=fixed['settings']['near_stream' if mode == 'hold_stream' else 'manual'])
        env = {k: v for k, v in os.environ.items() if k not in
            ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD', 'HTTP_PROXY', 'HTTPS_PROXY',
             'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'NODE_USE_ENV_PROXY')}
        env['Q4T_NODE_SDK_DIR'] = self.stage.plan['sdk']['sdk_directory']
        self.stderr = (self.stage.output/(prefix+'.stderr')).open('xb')
        self.process = subprocess.Popen([self.stage.plan['sdk']['node']['path'],
            str(ROOT/'tools/serve_realtime/recovery_sdk.mjs')], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.stderr, env=env)
        self.buffer.clear(); self.total_bytes = 0
        self.process.stdin.write(json.dumps(args).encode())
        self.process.stdin.close(); self.process.stdin = None
        if mode == 'complete':
            self.finish()
        else:
            ready = self.line(90)
            save(self.stage.output/(prefix+'-ready.json'), ready)
            require(ready['phase'] == 'ready' and ready['mode'] == mode,
                    'actual client not ready for fault')
            self.current['held'] = self.observe('held', lambda s:
                s['sessions'] == 1 and s['host_bytes'] > 0 and
                (s['active'] == 1 and s['reserved'] > 0 if mode == 'hold_stream' else
                 s['active'] == s['reserved'] == 0 and
                 s['success'] == self.current['before']['success']+1))

    def finish(self):
        require(self.current is not None, 'managed fault without owned connection')
        prefix = self.current['prefix']; mode = self.current['mode']
        value = self.line(90 if mode == 'complete' else 10)
        save(self.stage.output/(prefix+'.sdk.json'), value)
        self.process.wait(timeout=5)
        extra = self.process.stdout.read(1)
        require(self.process.returncode == 0 and not self.buffer and not extra,
                'managed client failed or emitted duplicate receipt')
        check_session(value, mode, 'openai-realtime-wss' if
                      self.stage.plan['scenario'] == 'tls' else 'ws-loopback')
        self.current['after'] = None if mode == 'hold_complete' else self.observe('after', clean)
        save(self.stage.output/(prefix+'-receipt.json'), self.current)
        self.process.stdout.close(); self.stderr.close()
        self.process = self.stderr = self.current = None

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=5)
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.stdout.close()
        if self.stderr is not None:
            self.stderr.close()
