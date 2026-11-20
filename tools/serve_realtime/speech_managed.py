"""Actual speech across native ownership changes, without replay or model control."""
import json
import os
from pathlib import Path
import subprocess

from tools.serve_realtime import managed, speech_checks
from tools.serve_recovery import profile_speech_evidence
from tools.serve_realtime.conversation_managed import owner
from tools.serve_service.package import read, require, same_json, save, sha

FORMAT = 'q4t-native-speech-recovery-v1'


def phases(plan):
    require(plan['speech_recovery']['format'] == FORMAT, 'unknown speech recovery contract')
    return {
        'tool_retained': [('primary','complete')],
        'tls': [('tls-before','hold_stream'),('tls-after','complete'),('tls-controller','complete')],
        'tool_version': [('primary','hold_complete'),('fallback','complete')],
    }[plan['scenario']]


def check_session(value, mode, transport):
    require(mode in ('complete','hold_stream','hold_complete') and
        transport in ('official-openai-wss','ws-loopback') and
        value.get('phase') == 'result' and value.get('status') == 'PASS' and
        value.get('mode') == mode and value.get('transport') == transport and
        value.get('connections') == 1 and value.get('automatic_replay') is False and
        value.get('response_creates') == (2 if mode == 'hold_stream' else 1) and
        type(value.get('close_code')) is int and value['close_code'] in
            ([1000] if mode == 'complete' else [1001,1006,1011,1012]),
        'actual speech transport/terminal differs')
    rows, events = value['responses'], value['events']
    require(len(rows) == len(value['retrievals']) == 1 and
        type(events) is list and 0 < len(events) <= 4096 and
        type(value['event_bytes']) is int and 0 < value['event_bytes'] <= 4 << 20 and
        type(value['sent_events']) is int and 0 < value['sent_events'] <= 32 and
        not any(e['type'] == 'error' for e in events) and
        len({e['event_id'] for e in events}) == len(events), 'speech event bounds, error or replay')
    updates=[e for e in events if e['type']=='session.updated']
    require(sum(e['type'] == 'session.created' for e in events) == 1 and
        len(updates) == (2 if mode=='hold_stream' else 1),
        'speech session reconfigured or restarted')
    settings = updates[0]['session']
    require(settings['output_modalities'] == ['audio'] and settings['max_output_tokens'] == 3457 and
        settings['audio']['output']['voice'] == 'ryan' and settings['audio']['output']['speed'] == 1 and
        settings['audio']['output']['format'] == dict(type='audio/pcm',rate=24000),
        'effective speech settings changed')
    users=[e for e in events if e['type']=='conversation.item.done' and
           e.get('item',{}).get('id')=='user_speech']
    require(len(users)==1 and users[0]['item']['role']=='user' and
        users[0]['item']['content']==[dict(type='input_text',
            text='Repeat exactly without adding words: '+speech_checks.PHRASE)],
        'current speech user task missing')
    require([e['response'] for e in events if e['type']=='response.done']==rows,
        'speech response omitted, completed after fault or replayed')
    row=rows[0];_,audio=speech_checks.audio(events,row,value['retrievals'][0])
    output=row['audio']['output']
    require(output.get('voice','ryan')=='ryan' and output.get('speed',1)==1,
            'contradictory response voice/speed echo')
    require((audio['bytes'],audio['sha256'])==profile_speech_evidence.PCM['builtin'] and
        audio['transcript']==speech_checks.PHRASE and audio['format']=='audio/pcm',
        'actual whole speech differs from independently recognized waveform')
    partial=value['partial'];identifiers=[row['id']]
    if mode=='hold_stream':
        slow=updates[1]['session']
        require(slow==dict(settings,audio=dict(settings['audio'],output=dict(
            settings['audio']['output'],speed=.25))), 'partial speech changed unrelated session settings')
        require(type(partial) is dict and partial.keys()=={'response_id','item_id','delta'} and
            all(type(v) is str and v for v in partial.values()) and
            partial['response_id'] != row['id'], 'real partial speech identity missing')
        deltas=[e for e in events if e['type']=='response.output_audio.delta' and
                e['response_id']==partial['response_id']]
        require(deltas and deltas[0]['delta']==partial['delta'] and
            all(e['item_id']==partial['item_id'] for e in deltas) and
            len(speech_checks.decode64(partial['delta']))>0,
            'interrupted response had no actual audio')
        identifiers.append(partial['response_id'])
        require(next(i for i,e in enumerate(events) if e['type']=='response.done') <
            events.index(updates[1]) < next(i for i,e in enumerate(events) if
                e['type']=='response.created' and e['response']['id']==partial['response_id']),
            'speech speed changed while a response was still running')
    else:require(partial is None,'unexpected partial speech')
    created=[e['response']['id'] for e in events if e['type']=='response.created']
    require(created==identifiers and events.index(users[0])<next(i for i,e in enumerate(events)
            if e['type']=='response.created'), 'missing, late or repeated actual speech response')
    require(type(value['transport_errors']) is list and len(value['transport_errors'])<=4 and
        (mode!='complete' or not value['transport_errors']), 'unexpected transport failure')
    return row['usage']['total_tokens']


def accounting(before, after, total, interrupted=False):
    managed.state_shape(before);managed.state_shape(after)
    count=2 if interrupted else 1
    require(after['admitted']==before['admitted']+count and
        after['total']==before['total']+count and after['errors']==before['errors'] and
        after['success']==before['success']+1 and
        after['aborted']==before['aborted']+int(interrupted),
        'speech work duplicated, lost or incorrectly terminated')
    used=after['settled']-before['settled']
    require(used>=total if interrupted else used==total,'actual speech TPM settlement differs')
    return used


def check(out, plan):
    out=Path(out);result={};identifiers=[]
    transport='official-openai-wss' if plan['scenario'].startswith('tls') else 'ws-loopback'
    for label,mode in phases(plan):
        prefix=label+'-speech';receipt=read(out/(prefix+'-receipt.json'))
        raw=read(out/(prefix+'.sdk.json'));total=check_session(raw,mode,transport)
        require(receipt['mode']==mode and managed.clean(receipt['before']), 'unclean initial speech state')
        for suffix in ('before','after')+(() if mode=='complete' else ('held',)):
            if suffix=='after' and mode=='hold_complete':
                require(receipt['after'] is None,'no rate subtraction across model replacement');continue
            samples=read(out/(prefix+'-'+suffix+'-metrics.json'))
            require(0<len(samples)<=128 and all(r['http_status']==200 for r in samples) and
                same_json(managed.state(samples[-1]['body']),receipt[suffix]), 'actual speech metrics differ')
        if mode!='complete':
            ready=read(out/(prefix+'-ready.json'));held=receipt['held'];managed.state_shape(held)
            require(ready.get('phase')=='ready' and ready.get('mode')==mode and
                ready.get('transport')==transport and ready.get('connections')==1 and
                ready.get('response_creates')==raw['response_creates'] and
                ready.get('partial')==raw['partial'] and
                ready.get('completed_ids')==[row['id'] for row in raw['responses']] and
                held['sessions']==1 and held['host_bytes']>0 and
                held['success']==receipt['before']['success']+1,
                'fault did not own the observed speech connection')
            require(held['active']==1 and held['reserved']>0 if mode=='hold_stream' else
                held['active']==held['reserved']==0, 'fault did not match actual speech state')
        after=receipt['held'] if mode=='hold_complete' else receipt['after']
        if mode!='hold_complete':require(managed.clean(after),'speech resources not drained')
        used=accounting(receipt['before'],after,total,mode=='hold_stream')
        before=read(out/(prefix+'-core-before.json'))
        last=read(out/(prefix+'-core-'+('held' if mode=='hold_complete' else 'after')+'.json'))
        require(before.get('business_ready') is True and owner(before)==owner(last),
                'speech or auxiliary work changed healthy model ownership')
        identifiers += [row['id'] for row in raw['responses']]
        if raw['partial']:identifiers.append(raw['partial']['response_id'])
        result[prefix]=dict(mode=mode,transport=transport,actual_tokens=used,
            completed_tokens=total,interrupted_usage_from_server=mode=='hold_stream',
            automatic_replay=False,core_owner=owner(before))
    require(len(identifiers)==len(set(identifiers)) and
        read(out/'counts.json')['counts']['websocket_sessions']==plan['limits']['websocket_sessions']==len(result),
        'speech session count or response identity replay')
    return result


class Actor(managed.Actor):
    def start(self,label):
        expected=phases(self.stage.plan)
        require(self.current is None and len(self.attempted)<len(expected) and
            label==expected[len(self.attempted)][0],'speech phase order or replay')
        mode=expected[len(self.attempted)][1];self.attempted.append(label)
        prefix=label+'-speech';self.current=dict(prefix=prefix,mode=mode)
        save(self.stage.output/(prefix+'-core-before.json'),self.stage.sample())
        self.current['before']=self.observe('before',managed.clean)
        self.stage.budget.reserve('websocket_sessions',prefix)
        for _ in range(2 if mode=='hold_stream' else 1):self.stage.budget.reserve('chats',prefix)
        tls=self.stage.plan['scenario'].startswith('tls')
        args=dict(base_url=f"{'https' if tls else 'http'}://127.0.0.1:{self.stage.port}/v1",
            api_key=self.stage.keys['alpha'],mode=mode,
            ca_file=self.stage.plan['tls_inputs']['ca']['path'] if tls else None)
        env={k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD',
            'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy','NODE_USE_ENV_PROXY')}
        env['Q4T_NODE_SDK_DIR']=self.stage.plan['sdk']['sdk_directory']
        self.stderr=(self.stage.output/(prefix+'.stderr')).open('xb')
        self.process=subprocess.Popen([self.stage.plan['sdk']['node']['path'],
            str(managed.ROOT/'tools/serve_realtime/speech_recovery_sdk.mjs')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.stderr,env=env)
        self.buffer.clear();self.total_bytes=0
        self.process.stdin.write(json.dumps(args).encode());self.process.stdin.close();self.process.stdin=None
        if mode=='complete':self.finish()
        else:
            ready=self.line(120);save(self.stage.output/(prefix+'-ready.json'),ready)
            require(ready.get('phase')=='ready' and ready.get('mode')==mode,'real speech not ready for fault')
            self.current['held']=self.observe('held',lambda s:s['sessions']==1 and s['host_bytes']>0 and
                (s['active']==1 and s['reserved']>0 if mode=='hold_stream' else
                 s['active']==s['reserved']==0 and s['success']==self.current['before']['success']+1))
            save(self.stage.output/(prefix+'-core-held.json'),self.stage.sample())

    def finish(self):
        require(self.current is not None,'speech fault without an owned connection')
        prefix=self.current['prefix'];mode=self.current['mode']
        value=self.line(120 if mode=='complete' else 10);save(self.stage.output/(prefix+'.sdk.json'),value)
        self.process.wait(timeout=5);extra=self.process.stdout.read(1)
        require(self.process.returncode==0 and not self.buffer and not extra,'speech SDK failed/repeated receipt')
        check_session(value,mode,'official-openai-wss' if self.stage.plan['scenario'].startswith('tls') else 'ws-loopback')
        self.current['after']=None if mode=='hold_complete' else self.observe('after',managed.clean)
        if mode!='hold_complete':save(self.stage.output/(prefix+'-core-after.json'),self.stage.sample())
        save(self.stage.output/(prefix+'-receipt.json'),self.current)
        self.process.stdout.close();self.stderr.close();self.process=self.stderr=self.current=None

    def finish_phases(self):
        require(self.current is None and self.attempted==[n for n,_ in phases(self.stage.plan)],
                'speech recovery phases incomplete')
