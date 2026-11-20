"""Actual same-conversation input, ownership and settlement across native faults."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from tools.serve_realtime import managed, duplex_checks as c, duplex_task_checks
from tools.serve_recovery import profile_evidence
from tools.serve_realtime.conversation_managed import owner
from tools.serve_service.package import read, require, same_json, save, sha

FORMAT = 'q4t-native-duplex-recovery-v1'
PHRASE = '甚至出现交易几乎停滞的情况。'


def phases(plan):
    require(plan['duplex_recovery']['format'] == FORMAT, 'unknown duplex recovery')
    return {
        'tool_retained': [('primary', 'complete')],
        'tls': [('tls-before', 'hold_stream'), ('tls-after', 'complete'),
                ('tls-controller', 'complete')],
        'tool_version': [('primary', 'hold_complete'), ('fallback', 'complete')],
    }[plan['scenario']]


def check_session(value, mode, transport, journal):
    c.journal_matches(journal, value)
    require(mode in ('complete', 'hold_stream', 'hold_complete') and
        transport in ('official-openai-wss', 'ws-loopback') and
        value.get('phase') == 'result' and value.get('status') == 'PASS' and
        value.get('mode') == mode and value.get('transport') == transport and
        value.get('connections') == 1 and value.get('automatic_replay') is False and
        value.get('response_creates') == 1 and
        value.get('input_sha256') == profile_evidence.REALTIME_INPUT_SHA and
        value.get('complete_audio_bytes') == 201790 and
        type(value.get('close_code')) is int and value['close_code'] in
            ([1000] if mode == 'complete' else [1001, 1006, 1011, 1012]),
        'actual duplex transport/input/terminal differs')
    rows, events = value['responses'], value['events']
    require(len(rows) == len(value['retrievals']) == 1 and
        0 < len(events) <= 4096 and 0 < value['event_bytes'] <= 4 << 20 and
        0 < value['sent_events'] <= 64 and
        not any(e['type'] == 'error' for e in events) and
        len({e['event_id'] for e in events}) == len(events),
        'duplex event bounds, error or replay')
    updates = [e for e in events if e['type'] == 'session.updated']
    require(sum(e['type'] == 'session.created' for e in events) == 1 and
        len(updates) == (2 if mode == 'hold_stream' else 1),
        'duplex session reconfigured or restarted')
    settings = updates[0]['session']
    expected_input = dict(format=dict(type='audio/pcm', rate=24000),
        transcription=dict(model='qwen3-asr-0.6b', language='', prompt='',
                           languages=[], keywords=[]),
        turn_detection=None, noise_reduction=None)
    require(settings['output_modalities'] == ['text'] and
        settings['max_output_tokens'] == 128 and
        settings['instructions'] == duplex_task_checks.INSTRUCTIONS and
        settings['audio']['input'] == expected_input,
        'effective input settings changed or transcription hinted')
    commits = [e for e in events if e['type'] == 'input_audio_buffer.committed']
    require(len(commits) == 1 and commits[0]['previous_item_id'] is None,
            'fresh input commit missing or replayed')
    item_id = commits[0]['item_id']
    input_tokens, quality = c.input_rows(events, [(item_id, 0)], PHRASE)
    fetched = value['retrievals'][0]
    require(fetched['id'] == item_id and fetched['role'] == 'user' and
        len(fetched['content']) == 1 and
        any(e['type'] == 'conversation.item.retrieved' and e['item'] == fetched
            for e in events), 'retrieved input owner differs')
    part = fetched['content'][0]
    waveform = c.speech_checks.decode64(part['audio'])
    require(part['type'] == 'input_audio' and part['transcript'] == PHRASE and
        len(waveform) == 201788 and hashlib.sha256(waveform).hexdigest() ==
        profile_evidence.REALTIME_WAVEFORM_SHA, 'actual processed audio differs')
    row = rows[0]
    require([e['response'] for e in events if e['type'] == 'response.done'] == rows and
        [e['response']['id'] for e in events if e['type'] == 'response.created'] ==
        [row['id']] and row['status'] == 'completed' and
        row['output_modalities'] == ['text'], 'response omitted or replayed')
    delta = ''.join(e['delta'] for e in events if
        e['type'] == 'response.output_text.delta' and e['response_id'] == row['id'])
    require(delta == c.text(row), 'stream differs from terminal')
    quality.append(c.judgment('response', c.text(row), PHRASE))
    require(all(q['status'] == 'PASS' for q in quality),
            'actual duplex task quality failed: ' + json.dumps(quality, ensure_ascii=False))
    client = [r['event'] for r in journal if r['direction'] == 'client']
    creates = [e['response'] for e in client if e['type'] == 'response.create']
    require(len(creates) == 1 and creates[0]['conversation'] == 'auto' and
        'input' not in creates[0] and creates[0]['instructions'] ==
        duplex_task_checks.INSTRUCTIONS and
        not any(e['type'] in ('conversation.item.create', 'response.cancel')
                for e in client), 'answer did not consume the native input history')
    sent = [c.speech_checks.decode64(e['audio']) for e in client
            if e['type'] == 'input_audio_buffer.append']
    require(sum(map(len, sent)) == 201790 + value['partial_audio_bytes'] and
        sum(e['type'] == 'input_audio_buffer.commit' for e in client) == 1 and
        len(client) == value['sent_events'], 'actual input attempts differ')
    raw = b''.join(sent)
    require(hashlib.sha256(raw[:201790]).hexdigest() ==
        profile_evidence.REALTIME_INPUT_SHA, 'actual sent input differs')
    partial = value['partial']
    if mode == 'hold_stream':
        require(updates[1]['session'] == dict(settings, audio=dict(settings['audio'],
            input=dict(expected_input, transcription=dict(
                expected_input['transcription'], delay='low')))),
            'partial ASR changed unrelated settings')
        require(type(partial) is dict and partial.keys() == {'item_id', 'delta'} and
            partial['item_id'] != item_id and value['partial_audio_bytes'] == 144000 and
            raw[201790:] == raw[:144000], 'missing actual withheld continuous input')
        deltas = [e for e in events if e['type'] ==
            'conversation.item.input_audio_transcription.delta' and
            e['item_id'] == partial['item_id']]
        require(deltas and deltas[0]['delta'] == partial['delta'] and
            partial['delta'] and PHRASE.startswith(''.join(e['delta'] for e in deltas)),
            'partial native ASR not observed')
        require(events.index(next(e for e in events if e['type'] == 'response.done')) <
            events.index(updates[1]) < events.index(deltas[0]),
            'fault did not follow complete input and reply')
        require(not any(e['type'].startswith('conversation.item.') and
            e.get('item', {}).get('id') == partial['item_id'] for e in events),
            'withheld uncommitted input entered public history')
    else:
        require(partial is None and value['partial_audio_bytes'] == 0,
                'unexpected partial input')
    require(type(value['transport_errors']) is list and
        len(value['transport_errors']) <= 4 and
        (mode != 'complete' or not value['transport_errors']), 'transport failure')
    return dict(tokens=input_tokens + c.speech_checks.usage(row),
                ids=[item_id, row['id']] + ([partial['item_id']] if partial else []),
                quality=quality)


def accounting(before, after, total, interrupted=False):
    managed.state_shape(before)
    managed.state_shape(after)
    count = 2 + int(interrupted)
    require(after['admitted'] == before['admitted'] + count and
        after['total'] == before['total'] + count and
        after['errors'] == before['errors'] and
        after['success'] == before['success'] + 2 and
        after['aborted'] == before['aborted'] + int(interrupted),
        'duplex work duplicated, lost or incorrectly terminated')
    used = after['settled'] - before['settled']
    require(used > total if interrupted else used == total,
            'actual native input/response settlement differs')
    return used


def journal(out, prefix):
    path = Path(out)/(prefix+'.events.jsonl')
    require(path.stat().st_size <= 8 << 20, 'bounded original journal')
    return [json.loads(line) for line in path.read_text().splitlines()]


def check(out, plan):
    out = Path(out)
    result, identifiers = {}, []
    transport = 'official-openai-wss' if plan['scenario'] == 'tls' else 'ws-loopback'
    for label, mode in phases(plan):
        prefix = label+'-duplex'
        receipt = read(out/(prefix+'-receipt.json'))
        raw = read(out/(prefix+'.sdk.json'))
        observed = check_session(raw, mode, transport, journal(out, prefix))
        require(receipt['mode'] == mode and managed.clean(receipt['before']),
                'unclean initial duplex state')
        for suffix in ('before', 'after') + (() if mode == 'complete' else ('held',)):
            if suffix == 'after' and mode == 'hold_complete':
                require(receipt['after'] is None, 'no subtraction across model replacement')
                continue
            samples = read(out/(prefix+'-'+suffix+'-metrics.json'))
            require(0 < len(samples) <= 128 and all(r['http_status'] == 200 for r in samples)
                and same_json(managed.state(samples[-1]['body']), receipt[suffix]),
                'actual duplex metrics differ')
        if mode != 'complete':
            ready = read(out/(prefix+'-ready.json'))
            held = receipt['held']
            managed.state_shape(held)
            require(ready.get('phase') == 'ready' and ready.get('mode') == mode and
                ready.get('transport') == transport and ready.get('connections') == 1 and
                ready.get('partial') == raw['partial'] and ready.get('response_creates') == 1
                and ready.get('completed_ids') == [r['id'] for r in raw['responses']] and
                ready.get('input_item_id') == observed['ids'][0] and
                ready.get('partial_audio_bytes') == raw['partial_audio_bytes'] and
                held['sessions'] == 1 and held['host_bytes'] > 0 and
                held['success'] == receipt['before']['success'] + 2,
                'fault did not own the observed duplex work')
            require(held['active'] == 1 and held['reserved'] > 0 if mode == 'hold_stream'
                else held['active'] == held['reserved'] == 0,
                'fault did not match actual input work state')
        after = receipt['held'] if mode == 'hold_complete' else receipt['after']
        if mode != 'hold_complete':
            require(managed.clean(after), 'duplex resources not drained')
        used = accounting(receipt['before'], after, observed['tokens'], mode == 'hold_stream')
        first = read(out/(prefix+'-core-before.json'))
        last = read(out/(prefix+'-core-'+('held' if mode == 'hold_complete' else 'after')+'.json'))
        require(first.get('business_ready') is True and owner(first) == owner(last),
                'input work changed healthy model ownership')
        identifiers += observed['ids']
        result[prefix] = dict(mode=mode, transport=transport, actual_tokens=used,
            completed_tokens=observed['tokens'], interrupted_usage_from_server=mode == 'hold_stream',
            automatic_replay=False, core_owner=owner(first), quality=observed['quality'])
    require(len(identifiers) == len(set(identifiers)) and
        read(out/'counts.json')['counts']['websocket_sessions'] ==
        plan['limits']['websocket_sessions'] == len(result), 'session count or identity replay')
    return result


class Actor(managed.Actor):
    def start(self, label):
        expected = phases(self.stage.plan)
        require(self.current is None and len(self.attempted) < len(expected) and
            label == expected[len(self.attempted)][0], 'duplex phase order or replay')
        mode = expected[len(self.attempted)][1]
        self.attempted.append(label)
        prefix = label+'-duplex'
        self.current = dict(prefix=prefix, mode=mode)
        save(self.stage.output/(prefix+'-core-before.json'), self.stage.sample())
        self.current['before'] = self.observe('before', managed.clean)
        self.stage.budget.reserve('websocket_sessions', prefix)
        for _ in range(3 if mode == 'hold_stream' else 2):
            self.stage.budget.reserve('chats', prefix)
        tls = self.stage.plan['scenario'] == 'tls'
        clip = self.stage.plan['duplex_recovery']['clip']
        require(sha(clip['path']) == clip['sha256'] == profile_evidence.REALTIME_INPUT_SHA,
                'actual recovery input drift')
        args = dict(base_url=f"{'https' if tls else 'http'}://127.0.0.1:{self.stage.port}/v1",
            api_key=self.stage.keys['alpha'], mode=mode, clip=clip['path'],
            clip_sha256=clip['sha256'],
            journal_file=str(self.stage.output/(prefix+'.events.jsonl')),
            ca_file=self.stage.plan['tls_inputs']['ca']['path'] if tls else None)
        env = {k:v for k,v in os.environ.items() if k not in
            ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD','HTTP_PROXY','HTTPS_PROXY',
             'ALL_PROXY','http_proxy','https_proxy','all_proxy','NODE_USE_ENV_PROXY')}
        env['Q4T_NODE_SDK_DIR'] = self.stage.plan['sdk']['sdk_directory']
        self.stderr = (self.stage.output/(prefix+'.stderr')).open('xb')
        self.process = subprocess.Popen([self.stage.plan['sdk']['node']['path'],
            str(managed.ROOT/'tools/serve_realtime/duplex_recovery_sdk.mjs')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, env=env)
        self.buffer.clear()
        self.total_bytes = 0
        self.process.stdin.write(json.dumps(args).encode())
        self.process.stdin.close()
        self.process.stdin = None
        if mode == 'complete':
            self.finish()
        else:
            ready = self.line(120)
            save(self.stage.output/(prefix+'-ready.json'), ready)
            require(ready.get('phase') == 'ready' and ready.get('mode') == mode,
                    'real duplex input not ready for fault')
            self.current['held'] = self.observe('held', lambda s:s['sessions'] == 1 and
                s['host_bytes'] > 0 and s['success'] == self.current['before']['success'] + 2
                and (s['active'] == 1 and s['reserved'] > 0 if mode == 'hold_stream' else
                     s['active'] == s['reserved'] == 0))
            save(self.stage.output/(prefix+'-core-held.json'), self.stage.sample())

    def finish(self):
        require(self.current is not None, 'fault without owned duplex connection')
        prefix, mode = self.current['prefix'], self.current['mode']
        value = self.line(120 if mode == 'complete' else 10)
        save(self.stage.output/(prefix+'.sdk.json'), value)
        self.process.wait(timeout=5)
        extra = self.process.stdout.read(1)
        require(self.process.returncode == 0 and not self.buffer and not extra,
                'duplex SDK failed/repeated receipt')
        check_session(value, mode, 'official-openai-wss' if
            self.stage.plan['scenario'] == 'tls' else 'ws-loopback',
            journal(self.stage.output, prefix))
        self.current['after'] = None if mode == 'hold_complete' else self.observe('after', managed.clean)
        if mode != 'hold_complete':
            save(self.stage.output/(prefix+'-core-after.json'), self.stage.sample())
        save(self.stage.output/(prefix+'-receipt.json'), self.current)
        self.process.stdout.close()
        self.stderr.close()
        self.process = self.stderr = self.current = None

    def finish_phases(self):
        require(self.current is None and self.attempted == [n for n,_ in phases(self.stage.plan)],
                'duplex recovery phases incomplete')
