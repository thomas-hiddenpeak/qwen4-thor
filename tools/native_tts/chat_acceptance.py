"""Bounded real Chat audio, SDK, tool/history, accounting and cancellation."""
import argparse
import base64
import copy
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_tts import http_acceptance as speech
from tools.native_tts.acceptance import defaults
from tools.native_tts.utterance_acceptance import content_check, normalize
from tools.serve_service.package import read, require, save, sha

WORK, ASR, lifecycle = speech.WORK, speech.ASR, speech.lifecycle
LIMITS = dict(starts=2, chats=12, controls=2000, seconds=1200,
    gpu_talker_loads=1, gpu_codec_loads=1, gpu_asr_loads=2,
    decode_cases=6, asr_tokens_per_case=64, artifact_mib=64,
    disk_headroom_mib=512)
SECRET = '8' * 64  # Private loopback fixture identity, not a deployment secret.
HEADERS = {'X-Q4T-Client-Id': 'audio', 'X-Q4T-Gateway-Token': SECRET}
RECORD = 'docs/evidence/serve-speech-http-20261118/index.json'
EN = 'Hello, this is a native speech test.'
ZH = '你好，会议下午三点开始。'


def request(text, fmt='mp3', stream=False, voice='ryan', audio_only=False):
    value = dict(model='qwen3.8-flash-next', temperature=0,
        enable_thinking=False, max_completion_tokens=1536,
        messages=[dict(role='user', content=text)],
        modalities=['audio'] if audio_only else ['text', 'audio'],
        audio=dict(voice=voice, format=fmt), stream=stream)
    if stream:
        value['stream_options'] = dict(include_usage=True, include_obfuscation=False)
    return value


def requests():
    first = request('Repeat exactly this sentence without adding words: '+EN,
                    'wav', audio_only=True)
    first.pop('max_completion_tokens')  # Exercise the native audio default budget.
    first['store'] = True
    tool = request('Read the real speech acceptance record with read_record. '
        'After the result, reply exactly with its spoken_summary field.')
    tool.update(tool_choice='required', tools=[dict(type='function', function=dict(
        name='read_record', description='Read the project speech HTTP acceptance record.',
        parameters=dict(type='object', properties={}, required=[], additionalProperties=False),
        strict=True))])
    schema = request('Return the object with text equal to Hello.')
    schema.update(n=2, max_completion_tokens=1024,
        response_format=dict(type='json_schema', json_schema=dict(name='greeting', strict=True,
            schema=dict(type='object', properties={'text': dict(type='string', enum=['Hello'])},
                        required=['text'], additionalProperties=False))))
    return dict(json=first,
        python=request('请严格原样复述，不要添加其他文字：'+ZH, 'pcm16', True, 'vivian'),
        tool=tool, schema=schema,
        cancel=request('Repeat the following sentence twenty times, without added words: '+EN,
                       'mp3', True),
        core=dict(model='qwen3.8-flash-next', enable_thinking=False, temperature=0,
            max_completion_tokens=32, messages=[dict(role='user',
            content='Compute 17 + 25. Reply only with the decimal number.')]),
        speech=speech.speech(0))


def parse_stream(raw, count):
    lines = [line[6:] for line in raw.splitlines() if line.startswith(b'data: ')]
    require(lines and lines[-1] == b'[DONE]' and lines.count(b'[DONE]') == 1,
            'missing/duplicate Chat terminal')
    states = [dict(data='', transcript='', content='', id='', expires_at=0,
                   finish=None, audio_closed=False) for _ in range(count)]
    usage = None
    events = []
    for line in lines[:-1]:
        event = json.loads(line)
        require('error' not in event and event.get('object') == 'chat.completion.chunk',
                'Chat stream error or envelope')
        events.append(event)
        if event.get('usage') is not None:
            require(usage is None and not event['choices'], 'usage order')
            usage = event['usage']
        for choice in event['choices']:
            require(usage is None and type(choice['index']) is int and
                    0 <= choice['index'] < count, 'choice index/order')
            state = states[choice['index']]
            require(state['finish'] is None, 'delta after choice finish')
            delta = choice['delta']
            if delta.get('content') is not None: state['content'] += delta['content']
            audio = delta.get('audio')
            if audio is not None:
                require(not state['audio_closed'], 'audio after expiry')
                if 'expires_at' in audio:
                    require(set(audio) == {'expires_at'} and 'finish_reason' not in choice,
                            'last audio update must omit finish_reason')
                    require(state['id'] and state['data'] and state['transcript'],
                            'expired incomplete audio')
                    state['expires_at'] = audio['expires_at']; state['audio_closed'] = True
                else:
                    for key, value in audio.items():
                        require(key in ('id', 'data', 'transcript') and isinstance(value,str),
                                'audio delta shape')
                        if key == 'id': require(not state['id'], 'duplicate audio ID')
                        state[key] += value
            if choice.get('finish_reason') is not None:
                require(state['audio_closed'], 'choice finished before audio')
                state['finish'] = choice['finish_reason']
    require(usage is not None and all(s['finish'] == 'stop' for s in states),
            'incomplete audio choices or usage')
    for state in states:
        require(base64.b64decode(state['data'], validate=True), 'empty/corrupt audio')
    return states, usage, events


def check_usage(usage, audio_count):
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        require(type(usage.get(key)) is int and usage[key] > 0, 'missing actual usage')
    require(usage['prompt_tokens'] + usage['completion_tokens'] == usage['total_tokens'],
            'usage sum')
    synth = usage['q4t_audio_synthesis']
    codes = usage['completion_tokens_details']['audio_tokens']
    require(type(codes) is int and codes > 0 and codes % 16 == 0 and
            synth['input_tokens'] > 0 and synth['output_tokens'] == codes+audio_count and
            usage['prompt_tokens'] > synth['input_tokens'] and
            usage['completion_tokens'] > synth['output_tokens'], 'invented/double-counted usage')


def prepare(args):
    require(not subprocess.check_output(['git','status','--porcelain']), 'commit sources first')
    out = args.output.absolute()
    require(out.is_relative_to(ROOT/'.q4t-work'), 'work output only')
    out.mkdir(parents=True,mode=0o700,exist_ok=False)
    model = Path.home()/'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
    names = subprocess.check_output(['git','ls-files','src','include','cmake','CMakeLists.txt',
        'tools/native_tts','tools/native_asr','tools/serve_grammar_reap','tools/serve_protocol',
        'tools/serve_recovery','tools/serve_service','tools/evalscope','tools/examples',
        'tools/release',RECORD],text=True).splitlines()
    binaries=dict(engine=args.binary.absolute(),decoder=args.decoder.absolute(),
                  asr=ASR/'validated-binaries/q4t_asr_replay-compute-01')
    assets=WORK/'assets-1b7-01'
    plan=dict(schema='q4t-chat-audio-http-v1',output=str(out),limits=LIMITS,
        port=args.port,operations_port=args.port+1,model=str(model),
        model_assets=speech.assets(model),requests=requests(),
        sources={p:sha(ROOT/p) for p in names},
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        binaries={k:dict(path=str(v),sha256=sha(v)) for k,v in binaries.items()},
        defaults=defaults(),sdk=speech.sdk_identity(),python_sdk=speech.python_identity(),
        tts_directory=str(assets),tts_assets={str(p.relative_to(assets)):dict(
            bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(assets.rglob('*')) if p.is_file()},
        asr_assets={p.name:sha(p) for p in sorted((ASR/'assets-01').iterdir()) if p.is_file()},
        derived=['tool follow-up uses actual call/id and real acceptance record digest',
                 'history copies actual audio ID after one controlled clean restart',
                 'all six decoded utterances checked against actual source text, no ASR hints'],
        mutations=['cancel one owned audio stream after real data',
                   'one graceful owned model restart with the same private object directory'],
        acceptance=dict(exact_normalized_asr=True,default_unchanged=True,
            performance_claim=False,automatic_model_restart=False,managed_ingress_qualified=False))
    save(out/'plan.json',plan)
    print(json.dumps(dict(plan=str(out/'plan.json'),limits=LIMITS)))


class Stage(speech.Stage):
    def __init__(self,plan):
        require(not os.environ.get('PYTHONOPTIMIZE') and not any(
            key.startswith('Q4T_ABLATE_') for key in os.environ),'unqualified environment')
        self.plan,self.out=plan,Path(plan['output'])
        self.started=time.monotonic();self.limits=LIMITS
        self.counts=dict(starts=0,chats=0,controls=0,gpu_asr_loads=0)
        self.model=self.model_identity=self.fault_thread=None
        self.history={};self.rows=[];self.expected=[]
        self.lease=int(os.environ['Q4T_MANAGED_LEASE_FD'])
        lifecycle.verify_lease(self.lease,lifecycle.guard.common_lock())
        require(plan['schema']=='q4t-chat-audio-http-v1' and plan['limits']==LIMITS and
                plan['requests']==requests(),'frozen plan drift')
        for p,h in plan['sources'].items(): require(sha(ROOT/p)==h,'source drift: '+p)
        for row in plan['binaries'].values(): require(sha(row['path'])==row['sha256'],'binary drift')
        require(defaults()==plan['defaults'],'default drift')
        require(speech.sdk_identity()==plan['sdk'] and
                speech.python_identity()==plan['python_sdk'],'SDK drift')
        speech.check_assets(plan['model'],plan['model_assets'])
        for name,h in plan['asr_assets'].items(): require(sha(ASR/'assets-01'/name)==h,'ASR drift')
        for name,row in plan['tts_assets'].items():
            path=Path(plan['tts_directory'])/name
            require(path.stat().st_size==row['bytes'] and sha(path)==row['sha256'],'TTS drift')
        self.artifacts()

    def budget(self,kind):
        require(time.monotonic()-self.started<LIMITS['seconds']-40,'stage deadline')
        require(self.counts[kind]<LIMITS[kind],'attempt budget: '+kind)
        self.counts[kind]+=1;save(self.out/'counts.json',self.counts)

    def artifacts(self):
        require(sum(p.stat().st_size for p in self.out.rglob('*') if p.is_file())
                <=LIMITS['artifact_mib']<<20,'artifact budget')
        require(shutil.disk_usage(ROOT).free>=LIMITS['disk_headroom_mib']<<20,'disk headroom')

    def http(self,path,body=None,*,label=None,timeout=180,client='audio',cancel=False):
        if body is None: self.budget('controls')
        local = body is None and path in (
            '/healthz', '/livez', '/readyz', '/metrics', '/v1/config')
        port = self.plan['operations_port'] if local else self.plan['port']
        headers = {} if local else dict(HEADERS, **{'X-Q4T-Client-Id': client})
        headers.update({'Content-Type': 'application/json', 'Connection': 'close'})
        conn=http.client.HTTPConnection('127.0.0.1',port,timeout=timeout)
        started=time.monotonic();raw=b'';chunks=0
        try:
            conn.request('GET' if body is None else 'POST',path,
                None if body is None else json.dumps(body,ensure_ascii=False).encode(),
                headers)
            response=conn.getresponse()
            while True:
                part=response.read1(8192)
                if not part: break
                raw+=part;chunks+=1
                require(len(raw)<=4<<20,'wire byte budget')
                if cancel and b'"data":' in raw:
                    require(response.status==200,'cancel before audio')
                    if conn.sock: conn.sock.shutdown(socket.SHUT_RDWR)
                    response.close();break
            if label:
                (self.out/(label+'.wire')).write_bytes(raw)
                save(self.out/(label+'.http.json'),dict(status=response.status,
                    headers=dict(response.getheaders()),chunks=chunks,bytes=len(raw),
                    seconds=time.monotonic()-started,cancelled=cancel))
            if cancel: require(b'"data":' in raw and b'[DONE]' not in raw,'not mid-audio cancel')
            return response.status,raw
        finally: conn.close()

    def post(self,label,body,**kwargs):
        self.budget('chats');save(self.out/(label+'.request.json'),body)
        return self.http('/v1/chat/completions',body,label=label,**kwargs)

    def start(self):
        for port in (self.plan['port'],self.plan['operations_port']):
            with socket.socket() as sock: sock.bind(('127.0.0.1',port))
        policy=dict(version=3,revision=0,fair_queue=True,gateway_token=SECRET,
            clients=[dict(id=name,max_inflight=1,max_control_inflight=4,
                max_prompt_tokens=8192,max_output_tokens=8192,max_body_bytes=1048576,
                requests_per_minute=32,tokens_per_minute=64 if name=='tiny' else 200000)
                for name in ('audio','other','tiny')])
        save(self.out/'client-policy.json',policy)
        args=[self.plan['binaries']['engine']['path'],'serve','--model-dir',self.plan['model'],
            '--host','127.0.0.1','--port',str(self.plan['port']),
            '--operations-port',str(self.plan['operations_port']),'--mtp','--max-seq','1',
            '--max-len','8192','--max-prefill','2048','--max-tokens','64',
            '--max-inflight-requests','4','--max-queued-requests','3',
            '--object-store-dir',str(self.out/'objects'),'--client-policy-file',str(self.out/'client-policy.json'),
            '--tts-model-dir',self.plan['tts_directory'],'--tts-max-text-tokens','512',
            '--tts-max-prefill-tokens','512','--tts-max-frames','160',
            '--tts-workspace-mib','2048','--tts-max-output-mib','16']
        self.budget('starts');name='model-'+str(self.counts['starts'])
        save(self.out/(name+'-command.json'),args)
        with (self.out/(name+'.log')).open('xb',buffering=0) as log:
            self.model=subprocess.Popen(args,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
                                        pass_fds=(self.lease,))
        self.model_identity=lifecycle.guard.processes()[self.model.pid]
        self.history[self.model.pid]=self.model_identity
        save(self.out/(name+'-identity.json'),dict(pid=self.model.pid,**self.model_identity))
        self.wait_ready()
        for path, label in (('/v1/capabilities', 'capabilities'), ('/v1/config', 'config')):
            status, _ = self.http(path, label=name+'-'+label)
            require(status == 200, 'startup observation failed: '+path)

    def wait_ready(self):
        end=time.monotonic()+180
        while time.monotonic()<end:
            self.same_model()
            try:
                status, raw = self.http('/readyz',timeout=1)
                if status == 200: return
                if status != 503:
                    save(self.out/'readiness-failure.json', dict(status=status,
                        body=raw[:1024].decode('utf-8', errors='replace')))
                    raise RuntimeError('unexpected readiness status: '+str(status))
            except (OSError,http.client.HTTPException): pass
            time.sleep(.1)
        raise TimeoutError('readiness')

    def sdk(self,label,body,python):
        self.budget('chats')
        value=dict(request=body,headers=HEADERS,base_url=f"http://127.0.0.1:{self.plan['port']}/v1",
            sdk_directory=self.plan['sdk']['sdk_directory'],output=str(self.out/(label+'.wire')))
        save(self.out/(label+'.request.json'),value)
        cmd=([self.plan['python_sdk']['python'],'-B',str(ROOT/'tools/native_tts/chat_sdk.py')]
             if python else ['node',str(ROOT/'tools/native_tts/chat_sdk.mjs')])
        child=subprocess.run(cmd,input=json.dumps(value),text=True,capture_output=True,
            timeout=180,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        (self.out/(label+'.sdk.log')).write_text(child.stdout+child.stderr)
        require(child.returncode==0,'SDK failed: '+label)
        result=json.loads(child.stdout);save(self.out/(label+'.sdk.json'),result)
        require(result['ok'] and len(result['exchanges'])==1,'SDK retried')
        raw=(self.out/(label+'.wire')).read_bytes()
        if body.get('stream'):
            _,_,events=parse_stream(raw,body.get('n',1))
            for native,sdk in zip(events,result['result']['events'],strict=True):
                require(native['choices']==sdk['choices'], 'SDK changed choice wire fields')
        else: require(json.loads(raw)==result['result'],'SDK changed JSON')
        return raw

    def capture(self,label,raw,fmt,expected,stream=False,audio_only=False):
        if stream:
            states,usage,_=parse_stream(raw,len(expected))
            choices=[dict(message=dict(content=s['content'],audio=s)) for s in states]
        else:
            value=json.loads(raw);choices=value['choices'];usage=value['usage']
            require(len(choices)==len(expected) and all(c['finish_reason']=='stop' for c in choices),
                    'Chat audio generation incomplete')
        check_usage(usage,len(choices))
        for index,(choice,text) in enumerate(zip(choices,expected,strict=True)):
            message=choice['message'];audio=message['audio']
            require(audio['id'].startswith('audio_') and
                    time.time()<audio['expires_at']<=time.time()+3601,'audio identity/expiry')
            require(normalize(audio['transcript'])==normalize(text),'LLM transcript differs')
            if audio_only: require(message['content'] is None,'audio-only content not null')
            else: require(message['content']==audio['transcript'],'text/audio differs')
            encoded=base64.b64decode(audio['data'],validate=True)
            path=self.out/(label+'-'+str(index)+'.'+fmt);path.write_bytes(encoded)
            self.rows.append(dict(path=str(path),sha256=sha(path),format=fmt))
            self.expected.append(audio['transcript'])
        save(self.out/'decoded-expectations.json',self.expected)
        self.drained(label+'-drain');self.artifacts()
        return choices[0]['message']['audio']['id']

    def business(self):
        status,raw=self.post('json',self.plan['requests']['json'])
        require(status==200,'JSON audio failed')
        audio_id=self.capture('json',raw,'wav',[EN],audio_only=True)
        stored=json.loads(raw)
        raw=self.sdk('python',self.plan['requests']['python'],True)
        self.capture('python',raw,'pcm',[ZH],stream=True)
        status,raw=self.post('tool',self.plan['requests']['tool'])
        require(status==200,'tool request failed')
        choice=json.loads(raw)['choices'][0];message=choice['message']
        require(choice['finish_reason']=='tool_calls' and not message.get('audio') and
                len(message.get('tool_calls',[]))==1,'tool-only audio turn invalid')
        call=message['tool_calls'][0]
        require(call['function']['name']=='read_record' and
                json.loads(call['function']['arguments'])=={},'tool call differs')
        record=read(ROOT/RECORD)
        require(record['summary']['status']=='PASS_SPEECH_HTTP_SDK_CONTENT' and
                sha(ROOT/RECORD)==self.plan['sources'][RECORD],'record changed/unaccepted')
        result=dict(path=RECORD,sha256=sha(ROOT/RECORD),
                    record=dict(status=record['summary']['status']),
                    spoken_summary='The speech checks passed.')
        save(self.out/'tool-result.json',result)
        follow=copy.deepcopy(self.plan['requests']['tool']);follow['tool_choice']='none'
        follow['messages'] += [message,dict(role='tool',tool_call_id=call['id'],
                                            content=json.dumps(result,ensure_ascii=False))]
        raw=self.sdk('javascript',follow,False)
        self.capture('javascript',raw,'mp3',['The speech checks passed.'])
        status,raw=self.post('schema',self.plan['requests']['schema'])
        require(status==200,'multiple structured audio failed')
        for choice in json.loads(raw)['choices']:
            require(json.loads(choice['message']['content'])=={'text':'Hello'},'schema content')
        self.capture('schema',raw,'mp3',['{"text":"Hello"}']*2)
        self.post('cancel',self.plan['requests']['cancel'],cancel=True)
        self.drained('cancel-drain')
        status,raw=self.post('core',self.plan['requests']['core'])
        require(status==200 and json.loads(raw)['choices'][0]['message']['content'].strip()=='42',
                'core after audio cancellation')
        self.budget('chats')
        body=self.plan['requests']['speech'];save(self.out/'speech.request.json',body)
        status,raw=self.http('/v1/audio/speech',body,label='speech')
        require(status==200 and raw,'Speech shared-rate regression')
        path=self.out/'speech.mp3';path.write_bytes(raw)
        self.rows.append(dict(path=str(path),sha256=sha(path),format='mp3'));self.expected.append(EN)
        self.drained('before-restart')
        self.cleanup();require(self.model.returncode==0,'graceful model stop failed')
        shutil.copy2(self.out/'cleanup.json',self.out/'restart-cleanup.json')
        self.start()
        history=dict(model='qwen3.8-flash-next',temperature=0,enable_thinking=False,
            max_completion_tokens=64,messages=[dict(role='assistant',audio=dict(id=audio_id)),
                dict(role='user',content='Repeat the previous assistant sentence exactly, no added words.')])
        status,raw=self.http('/v1/chat/completions/'+stored['id'],label='stored-audio')
        require(status==200 and json.loads(raw)['choices']==stored['choices'],
                'stored Chat audio changed across restart')
        self.budget('chats')
        preflight=dict(request=history,include_prompt=True)
        save(self.out/'history-preflight.request.json',preflight)
        status,raw=self.http('/v1/chat/preflight',preflight,label='history-preflight')
        require(status==200 and EN in json.loads(raw)['prompt'],
                'audio history preflight failed')
        status,raw=self.post('history',history)
        require(status==200,'history after restart failed')
        require(normalize(json.loads(raw)['choices'][0]['message']['content'])==normalize(EN),
                'history content not restored')
        status,raw=self.post('foreign-history',history,client='other')
        require(status==404 and json.loads(raw)['error']['code']=='audio_not_found','history scope leaked')
        status,raw=self.post('quota',self.plan['requests']['json'],client='tiny')
        require(status==429 and json.loads(raw)['error']['code']=='client_tpm_exhausted','TPM bypass')
        self.drained('final-drain')
        require(len(self.rows)==LIMITS['decode_cases'],'audio case budget')
        save(self.out/'decode-plan.json',dict(schema='q4t-speech-http-decode-v1',cases=self.rows))
        save(self.out/'decoded-expectations.json',self.expected)

    def content(self):
        self.command('decode',[self.plan['binaries']['decoder']['path'],'--speech-http-decode',
            str(self.out/'decode-plan.json'),str(self.out/'decoded')],60)
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONPATH=str(ASR/'reference-deps'),
                 CUDA_VISIBLE_DEVICES='',USE_TORCH='0')
        self.command('features',[str(ROOT/'.q4t-work/refenv/bin/python'),'-B',__file__,
            'features','--output',str(self.out)],60,env)
        checks=[]
        for group in range(LIMITS['gpu_asr_loads']):
            self.budget('gpu_asr_loads')
            self.command('asr-'+str(group),[self.plan['binaries']['asr']['path'],str(ASR/'assets-01'),
                str(self.out/'features'/f'plan-{group}.json'),str(self.out/f'asr-{group}')],180)
            for local in range(3):
                index=group*3+local;directory=self.out/f'asr-{group}'/f'case-{local}'
                receipt=read(directory/'result.json');text=(directory/'text.txt').read_text()
                try: check=content_check(text,dict(text=self.expected[index],
                    language='chinese' if index==1 else 'english'))
                except AssertionError as error: check=dict(passed=False,error=str(error),transcript=text)
                check['passed'] &= receipt['eos'] and 0<receipt['generated_tokens']<=64
                check.setdefault('expected',self.expected[index])
                checks.append(dict(case=index,**check))
        save(self.out/'content.json',checks)
        require(all(c['passed'] for c in checks),'actual Chat/Speech content differs')

    def run(self):
        result=dict(status='FAILED');previous=signal.getsignal(signal.SIGALRM)
        def expired(_signum,_frame): raise TimeoutError('Chat audio stage deadline')
        signal.signal(signal.SIGALRM,expired)
        signal.setitimer(signal.ITIMER_REAL,max(.01,LIMITS['seconds']-40-(time.monotonic()-self.started)))
        try:
            self.start();self.business();self.cleanup()
            require(self.model.returncode==0,'final graceful shutdown failed')
            self.content();result['status']='PASS_CHAT_AUDIO_BUSINESS_PENDING_LEASE'
        except BaseException:
            result['error']=traceback.format_exc();raise
        finally:
            signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,previous)
            try: self.cleanup()
            except BaseException:
                result.update(status='FAILED',cleanup_error=traceback.format_exc());raise
            finally:
                result.update(counts=self.counts,seconds=time.monotonic()-self.started,
                              defaults_unchanged=defaults()==self.plan['defaults'])
                if not result['defaults_unchanged']: result['status']='FAILED'
                save(self.out/'result.json',result)


def features(out):
    # Reuse the exact qualified CPU feature preparation, with only the number
    # of actual HTTP utterances changed; the ASR template has no target hint.
    import numpy as np
    import transformers
    from transformers import Qwen2TokenizerFast,WhisperFeatureExtractor
    require(transformers.__version__=='4.57.6','feature source drift')
    tokenizer=Qwen2TokenizerFast.from_pretrained(ASR/'assets-01',local_files_only=True)
    extractor=WhisperFeatureExtractor.from_dict(read(ASR/'assets-01/preprocessor_config.json'))
    template=tokenizer.encode('<|im_start|>system\n<|im_end|>\n<|im_start|>user\n'
        '<|audio_start|><|audio_pad|><|audio_end|><|im_end|>\n<|im_start|>assistant\n',add_special_tokens=False)
    require(template.index(151676)==9 and template.count(151676)==1,'ASR template')
    target=out/'features';target.mkdir();cases=[]
    for index in range(LIMITS['decode_cases']):
        directory=out/'decoded'/f'case-{index}';record=read(directory/'result.json')
        pcm=directory/'pcm.f32';data=np.fromfile(pcm,'<f4')
        require(record['worker_starts']==record['worker_reaped']==1 and
            record['samples']==data.size<=240000 and record['sample_rate']==16000 and
            sha(pcm)==record['decoded_sha256'],'decoded identity')
        require(np.isfinite(data).all() and float(np.max(np.abs(data)))>.01,'invalid speech')
        mel=extractor._np_extract_fbank_features(data[None,:],'cpu')[0];frames=data.size//160
        require(mel.shape==(128,frames) and np.isfinite(mel).all(),'mel features')
        tokens=frames//100*13+((frames%100)+7)//8
        path=target/f'case-{index}-mel.f32';mel.astype('<f4').tofile(path)
        cases.append(dict(frames=frames,samples=int(data.size),audio_tokens=tokens,audio_start=9,
            max_new_tokens=64,tokens=template[:9]+[151676]*tokens+template[10:],
            pcm=str(pcm),pcm_sha256=sha(pcm),mel=str(path),mel_sha256=sha(path)))
    for group in range(LIMITS['gpu_asr_loads']):
        save(target/f'plan-{group}.json',dict(cases=cases[group*3:group*3+3]))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='mode',required=True)
    prepare_parser=sub.add_parser('prepare')
    for name in ('binary','decoder','output'):
        prepare_parser.add_argument('--'+name,type=Path,required=True)
    prepare_parser.add_argument('--port',type=int,default=18484)
    sub.add_parser('run').add_argument('--plan',type=Path,required=True)
    sub.add_parser('features').add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.mode=='prepare': prepare(args)
    elif args.mode=='features': features(args.output)
    else:
        with lifecycle.child_subreaper(): Stage(read(args.plan)).run()


if __name__=='__main__': main()
