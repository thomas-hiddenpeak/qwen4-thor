"""Read-only reuse of a complete primary run with the VAD config-name failure.

No sockets, subprocesses or model work. The original FAILED result stays intact;
all business assertions run against its frozen requests, wires and observations.
"""
import base64
import copy
import hashlib
import json
from pathlib import Path

from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_realtime as realtime
from tools.serve_recovery import profile_semantic as semantic
from tools.serve_recovery import profile_voice_review as voice
from tools.serve_service import package
from tools.serve_service.package import read, require, save, sha

FORMAT = 'q4t-complete-primary-review-v1'


def original_environment(source):
    plan = read(source/'plan.json'); result = read(source/'result.json')
    require(result['status'] == 'FAILED' and result['error'].endswith("KeyError: 'native_vad_limits'\n") and
        'profile_semantic.run(self)' in result['error'] and plan.get('semantic_vad') is True and
        result['binary_sha256'] == plan['binary_sha256'] and
        result['counts']['starts'] == 1 and result['counts']['chats']*2 == plan['limits']['chats'] and
        result['counts']['websocket_sessions'] == 3 and result['counts']['ingress_starts'] == 2 and
        result['seconds'] < plan['limits']['seconds'], 'only complete primary config-name failure may be reused')
    lease = read(source/'lease/exit.json')
    require(lease['status'] == 'COMMAND_FAILED' and lease['child_returncode'] == 1 and
        lease['compute_backend'] == 'nvml' and lease['lease_inherited_by_child'] is True,
        'original primary strict lease differs')
    queries = [json.loads(row) for row in (source/'lease/nvml-queries.jsonl').read_text().splitlines()]
    require(queries and all(q['status'] == 'OK' and q['returncode'] == 0 for q in queries) and
        queries[-1]['pids'] == [], 'original primary monitoring missing or failed')
    cleanup = read(source/'cleanup.json')
    require(cleanup['remaining'] == [] and cleanup['model_exit'] == 0 and
        cleanup['ports_released'] is True and cleanup['lease_continuously_held'] is True,
        'original primary cleanup incomplete')
    return dict(status='ACCEPT_REVIEWED_PRIMARY_ENVIRONMENT', model_starts=1,
        requests=result['counts']['chats'], websocket_sessions=3, ingress_starts=2,
        seconds=result['seconds'], nvml_queries=len(queries),
        original_failure_preserved=True, lease_result_sha256=sha(source/'lease/exit.json'))


class RecordedPrimary:
    def __init__(self, source):
        self.source = source; self.plan = read(source/'plan.json')

    def wire(self, name):
        prefix = self.source/('primary-'+name)
        require(read(str(prefix)+'.http.json')['status'] == 200, 'recorded HTTP failed: '+name)
        raw = Path(str(prefix)+'.wire').read_bytes()
        require(len(raw) <= 4 << 20, 'recorded response exceeds bound')
        return raw

    def value(self, name):
        return json.loads(self.wire(name))

    def request(self, name, expected):
        require(package.same_json(read(self.source/('primary-'+name+'.request.json')),expected),
                'recorded request differs: '+name)
        return self.value(name)

    def metrics(self, name, expected):
        measured = realtime.state(realtime.encrypted.foreground.base.lifecycle.parse_metrics(
            self.wire(name)), realtime.encrypted.CLIENT)
        require(package.same_json(measured, expected), 'original metric receipt differs: '+name)

    def after_metrics(self, prefix, expected):
        paths = list(self.source.glob('primary-'+prefix+'-after-*.wire'))
        require(paths and len(paths) <= 128, 'original bounded after metrics absent')
        paths.sort(key=lambda p:int(p.stem.rsplit('-',1)[1]))
        self.metrics(paths[-1].stem.removeprefix('primary-'), expected)

    def audio(self, kind):
        identity = self.plan['identity']; fixed = identity['native_asr']
        name = 'asr-auxiliary' if kind == 'auxiliary' else kind
        clip = self.plan['speaker_audio' if kind == 'speaker' else
                         'separator_audio' if kind == 'separator' else 'asr_audio']
        raw = Path(clip['path']).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == clip['sha256'], 'original audio changed')
        request = dict(model=fixed['model'], response_format='json', include=['logprobs'])
        if kind == 'auxiliary':
            raw = evidence.auxiliary_audio_source(raw,identity)
            request = evidence.auxiliary_audio_request(identity)
        elif kind == 'speaker':request = evidence.speaker_request(identity)
        elif kind == 'separator':request = evidence.separator_request(identity)
        response = self.request(name,request); config = self.value(name+'-config')
        value = dict(source_base64=base64.b64encode(raw).decode(), request=request,
            response=response, health=config['native_asr_health'], limits=config['native_asr_limits'])
        if kind == 'auxiliary':
            names = evidence.audio_auxiliaries(identity)
            value['health'] = {n:config['native_'+n+'_health'] for n in names}
            value['limits'] = {n:package.asr_auxiliary_observed_limits(config,n) for n in names}
        elif kind in ('speaker','separator'):
            value.pop('source_base64');value.update(source_sha256=hashlib.sha256(raw).hexdigest(),
                health=config['native_'+kind+'_health'], limits=config['native_'+kind+'_limits'])
        return value

    def semantic(self):
        prefix = 'primary-semantic-'
        turns = [semantic.compact(read(self.source/(prefix+n+'.sdk.json')),i == 1)
                 for i,n in enumerate(('complete','continuation'))]
        before = realtime.state(realtime.encrypted.foreground.base.lifecycle.parse_metrics(
            self.wire('semantic-before')),realtime.encrypted.CLIENT)
        paths = sorted(self.source.glob(prefix+'after-*.wire'),
                       key=lambda p:int(p.stem.rsplit('-',1)[1]))
        require(paths and len(paths) <= 128, 'semantic after metrics missing')
        after = realtime.state(realtime.encrypted.foreground.base.lifecycle.parse_metrics(
            self.wire(paths[-1].stem.removeprefix('primary-'))),realtime.encrypted.CLIENT)
        return dict(input_sha256=self.plan['realtime']['clip']['sha256'],
            settings=evidence.semantic_settings(), turns=turns, before=before, after=after,
            **semantic.configuration(self.value('semantic-health')))

    def business(self):
        from tools.serve_recovery import profile_acceptance as driver
        rows = self.plan['requests']; identity = self.plan['identity']
        schema = self.request('schema',rows['schema'])
        call = self.request('tool',rows['tool']); message = evidence.message(call,'tool_calls')
        calls = message['tool_calls']
        require(len(calls) == 1 and calls[0]['function']['name'] == 'project_document' and
            json.loads(calls[0]['function']['arguments']) == dict(path=driver.DOCUMENT),
            'recorded tool request differs')
        document = driver.ROOT/driver.DOCUMENT
        require(sha(document) == self.plan['sources'][driver.DOCUMENT], 'original tool document changed')
        source = dict(path=driver.DOCUMENT,content=document.read_text())
        reply = dict(role='tool',tool_call_id=calls[0]['id'],content=json.dumps(dict(
            path=driver.DOCUMENT,sha256=sha(document),answer=sha(document)[:12])))
        follow = copy.deepcopy(rows['tool']); follow['tool_choice']='none'
        follow['messages'] += [message,reply]
        actor = voice.RecordedVoice(self.source,self.plan); recorded_voice = actor.run()
        require(actor.requests == 12 and actor.seen == {p.name.removeprefix('primary-voice-').removesuffix('.request.json')
            for p in self.source.glob('primary-voice-*.request.json')}, 'incomplete original voice checklist')
        value = dict(schema=schema,tool_call=call,tool_result=self.request('tool-result',follow),
            tool_reply=reply,tool_source=source,embedding=self.request('embedding',rows['embedding']),
            voice=recorded_voice,speaker=self.audio('speaker'),separator=self.audio('separator'),
            asr=self.audio('asr'),asr_auxiliary=self.audio('auxiliary'))
        if self.plan['profiles']['primary'].get('allow_media'):
            value['media'] = dict(request=rows['media'],response=self.request('media',rows['media']))
        tts = evidence.tts_requests(); config = self.value('tts-config')
        require(read(self.source/'primary-speech.request.json') == tts['speech'], 'original speech request differs')
        value['tts'] = dict(requests=tts,speech_base64=base64.b64encode(self.wire('speech')).decode(),
            chat_response=self.request('chat-output',tts['chat']),health=config['native_tts_health'],
            limits=config['native_tts_limits'],sampling_profile=self.plan['tts_sampling_profile'])
        require(read(self.source/'primary-speech-reload.request.json') == tts['speech'], 'original reload request differs')
        driver.check_tts_family_reload(self.value('speech-reload-before'),self.value('speech-reload-after'),
            value['tts']['health'],base64.b64encode(self.wire('speech-reload')).decode(),identity,
            sampling_profile=evidence.tts_sampling(value['tts']))
        source_audio = value['asr']['source_base64']
        value['chat_audio'] = evidence.chat_audio_witness(source_audio,
            self.request('chat-audio',evidence.chat_audio_request(source_audio)),self.value('chat-audio-config'))
        value['realtime'] = read(self.source/'primary-realtime-business.json')
        original_sdk = realtime.compact(read(self.source/'primary-realtime.sdk.json'))
        expected = realtime.retain(dict(input_sha256=self.plan['realtime']['clip']['sha256'],
            session=original_sdk,before=value['realtime']['before'],after=value['realtime']['after']))
        require(package.same_json(expected,value['realtime']), 'original Realtime raw SDK differs')
        self.metrics('realtime-before',expected['before']);self.after_metrics('realtime',expected['after'])
        value['semantic_vad'] = self.semantic()
        capabilities = self.value('capabilities')
        require(capabilities == read(self.source/'primary-capabilities.json') and
            self.value('config') == read(self.source/'primary-config.json'), 'original configuration projection differs')
        evidence.business(value,identity,self.plan['profiles']['primary'],capabilities)
        return value


def prepare(source, plan):
    source = Path(source).resolve(strict=True)
    require(source.is_relative_to(semantic.ROOT/'.q4t-work') and
        not Path(plan['output']).resolve().is_relative_to(source), 'distinct private review directory required')
    original = read(source/'plan.json')
    require(original.get('voice_icl') is True and original.get('semantic_vad') is True and
        all(package.same_json(original[k],plan[k]) for k in ('identity','profiles','requests','voice_clips')) and
        original['base']['settings'] == plan['base']['settings'] and
        original['binary_sha256'] == plan['binary_sha256'] == sha(plan['binary']),
        'original primary engine, capacity, requests or backends differ')
    for key in ('protected','native_assets'):
        for path,digest in original[key].items():require(sha(path) == digest,'original primary dependency changed')
    environment = original_environment(source)
    files = {str(p):sha(p) for p in source.rglob('*') if p.is_file()}
    value = RecordedPrimary(source).business()
    require(all(sha(p) == digest for p,digest in files.items()), 'read-only review modified original records')
    out = Path(plan['output'])
    for name in ('config','capabilities'):
        save(out/('primary-'+name+'.json'),read(source/('primary-'+name+'.json')))
    save(out/'primary-cleanup.json',read(source/'cleanup.json'))
    semantic.save_receipt(out/'primary-business.json',value)
    for name in ('primary-config.json','primary-capabilities.json','primary-cleanup.json','primary-business.json'):
        files[str(out/name)] = sha(out/name)
    return dict(format=FORMAT,source=str(source),files=files,environment=environment,
        source_commit=original['source_commit'],scope='completed primary only; one new light model required')


def verify(selected):
    require(selected['format'] == FORMAT,'retained primary format differs')
    for name,digest in selected['files'].items():require(sha(name) == digest,'retained primary evidence drift')
    require(original_environment(Path(selected['source'])) == selected['environment'],
            'retained primary environment changed')


def combined_environment(out, selected, current):
    verify(selected)
    save(out/'combined-environment.json',dict(current=current,original=selected['environment'],
        source=selected['source'],original_failure_preserved=True,separate_continuous_leases=True))
    return dict(current,nvml_queries=current['nvml_queries']+selected['environment']['nvml_queries'],
                lease_result_sha256=sha(out/'combined-environment.json'))
