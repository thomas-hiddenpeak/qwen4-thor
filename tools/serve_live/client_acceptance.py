"""One native model, one official Live TLS connection, four actual work items.

Reuse the exact native-TTS query recording from the retained failed close-tail
attempt; its synthesis and transcription passed, but the original session did
not close successfully. ASR still receives only audio. Client delegation reads
the actual project record and receives native speech; one ordinary Chat after
drain protects healthy core service. No replay, restart or default change.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import checks
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_realtime.ingress_fixture import certificate
from tools.serve_realtime.vad_acceptance import VAD, WEIGHT, DIGEST
from tools.serve_recovery import profile_acceptance as qualification
from tools.serve_service.package import read, require, save, sha

WORK = ROOT/'.q4t-work/serve-live-20261121'
LIMITS = dict(starts=1, ingress_starts=1, websocket_sessions=1,
              audio_inputs=1, responses=2, chats=1, controls=2000, seconds=600)
FORMAT = 'q4t-live-client-query-business-v3'
QUERY = '请查询当前项目版本号。'


def phone_query_matches(actual, expected):
    # The retained zh recording specifies the spoken words, not a writing system.
    # Compare each position, including the punctuation; do not fold other words.
    traditional = '請查詢當前項目版本號。'
    return (expected == '请查询当前项目版本号。' and len(actual) == len(expected)
            and all(a in (s, t) for a, s, t in zip(actual, expected, traditional)))


def retained_query(directory=None):
    source=directory or WORK/'client-business-02'
    names=('query-fixture.json','query-speech.http.json','query-speech.wire',
           'query-speech.request.json','fixture-before.json','fixture-drained.json',
           'client.events.jsonl','result.json','query.pcm')
    fixture=read(source/'query-fixture.json');path=source/'query.pcm'
    require(fixture['sha256']==sha(path)==
            'b0e25b48845225acd325f9647b347538fe085a74dd8b58826e9c9c951ab04483'
            and sha(source/'query-speech.wire')==sha(path),
            'exact retained synthesized waveform')
    require(fixture['bytes']==path.stat().st_size==138240 and
            fixture['rate']==24000 and not fixture['transcript_supplied_to_asr']
            and fixture['expected_transcript']==QUERY,
            'retained input audio provenance')
    http=read(source/'query-speech.http.json')
    require(http['status']==200 and http['headers']['Content-Type']=='audio/pcm'
            and http['bytes']==fixture['bytes'],'actual source synthesis HTTP')
    request=read(source/'query-speech.request.json')
    require(request['input']==QUERY and request['response_format']=='pcm',
            'explicit source query synthesis')
    before=read(source/'fixture-before.json');after=read(source/'fixture-drained.json')
    key=lambda n:n+'{client="beta"}'
    require(after[key('q4t_client_rate_admitted_total')]-before[key('q4t_client_rate_admitted_total')]==1
            and after[key('q4t_client_rate_settled_tokens_total')]>before[key('q4t_client_rate_settled_tokens_total')]
            and after[key('q4t_client_rate_reserved_tokens')]==0,
            'actual source synthesis settlement')
    journal=[json.loads(line) for line in (source/'client.events.jsonl').read_text().splitlines()]
    transcript=''.join(row['event']['delta'] for row in journal
        if row['direction']=='server' and row['event']['type']=='session.input_transcript.delta')
    require(transcript==QUERY and read(source/'result.json')['status']=='FAIL',
            'original successful transcription and failed phase preserved')
    return dict(path=str(path),sha256=sha(path),bytes=fixture['bytes'],rate=24000,
        expected_transcript=QUERY,transcript_supplied_to_asr=False,
        source_files={str(source/n):sha(source/n) for n in names},
        original_phase_status='FAIL',synthesis_and_transcription='PASS')


def prepare(out):
    require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
            'commit reviewed source and verifier before GPU')
    built = read(WORK/'native-build-02/result.json')
    require(built['status']=='BUILD_PASS' and built['sources_unchanged'] and
            all(r['returncode']==r['warnings']==0 for r in built['commands']),
            'formal warning-free runtime build')
    for n,h in read(WORK/'native-build-02/plan.json')['sources'].items():
        require(sha(ROOT/n)==h,'built native source drift')
    cpu=read(WORK/'client-driver-cpu-07/result.json')
    require(cpu['status']=='PASS','driver negative checks missing')
    for n,h in cpu['sources'].items():require(sha(ROOT/n)==h,'driver source drift')
    require(read(WORK/'binding-cpu-01/result.json')['status']=='PASS','binding CPU')
    require(read(WORK/'close-tail-cpu-01/result.json')['status']=='PASS','close-tail CPU')
    clip=retained_query()
    previous=base.WORK/'transcription-business-04/run'
    require(read(previous/'result.json')['status']=='PASS','retained source audio qualification')
    plan=read(previous/'plan.json');out.mkdir(mode=0o700,parents=True)
    require(shutil.disk_usage(ROOT).free>=64<<20,'bounded evidence disk headroom')
    policy=read(previous/'client-policy.json')
    policy['gateway_token']=hashlib.sha256(os.urandom(32)).hexdigest()
    for client in policy['clients']:
        client.update(requests_per_minute=3 if client['id']=='alpha' else 1,
                      tokens_per_minute=12000,max_output_tokens=3457)
    save(out/'client-policy.json',policy);(out/'client-policy.json').chmod(0o600)
    binary=ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build/q4t'
    ingress=ROOT/'.q4t-work/serve-file-search-20261112/host/q4t-ingress'
    require(sha(binary)==built['binary_sha256'] and sha(ingress)==built['ingress_sha256'],
            'exact compiled programs')
    tls=certificate(out/'credentials')
    tts=read(ROOT/'build/service/engine/manifest.json')['settings']['native_tts']
    sdk=base.WORK/'profile-sdk-01'
    record=ROOT/'docs/STATUS.md';version=re.search(r'默认\s*rev(\d+)',record.read_text())
    require(version is not None,'actual current project version')
    names=subprocess.check_output(['git','ls-files','src','include','cmake','CMakeLists.txt',
        'tools/serve_live','tools/serve_realtime','tools/serve_grammar_reap','tools/serve_recovery',
        'tools/serve_service','tools/evalscope','tools/native_asr','tools/native_tts','tools/release'],
        cwd=ROOT,text=True).splitlines()
    plan.update(schema=FORMAT,limits=LIMITS,output=str(out),scope=__doc__,
        port=18658,operations_port=18659,public_port=18660,binary=str(binary),
        binary_sha256=sha(binary),ingress=str(ingress),ingress_sha256=sha(ingress),
        native_tts=tts,tts_assets={str(p):sha(p) for p in sorted(Path(tts['directory']).rglob('*')) if p.is_file()},
        vad_directory=str(VAD),vad_assets={WEIGHT:DIGEST},
        sdk_directory=str(sdk),sdk_files={str(p):sha(p) for p in sorted((sdk/'node_modules').rglob('*')) if p.is_file()},
        sources={n:sha(ROOT/n) for n in names},protected={str(ROOT/n):sha(ROOT/n) for n in (
            'build/q4t','build/q4t.release.json','build/service/manifest.json',
            'build/service.release.json','build/service-native-state/ledger.json')},
        client_policy_sha256=sha(out/'client-policy.json'),tls=tls,
        tls_files={tls[n]:sha(tls[n]) for n in ('certificate','key')},reused_core_before={},
        record=dict(path='docs/STATUS.md',sha256=sha(record),version='rev'+version[1]),
        retained_query=clip,
        prior_attempts=[dict(directory=str(WORK/'client-business-01'),status='FAIL',
            starts=1,actual_work=2,seconds=113.32063404517248,original_answer='明白。'),
            dict(directory=str(WORK/'client-business-02'),status='FAIL',starts=1,
                 actual_work=4,seconds=77.92148964898661,
                 failure='close submitted silence and exhausted frozen RPM3')],
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip())
    save(out/'plan.json',plan)
    print(json.dumps(dict(plan=str(out/'plan.json'),limits=LIMITS)),flush=True)


class Stage(base.Stage):
    limits=LIMITS

    def __init__(self,plan):
        require(plan['schema']==FORMAT,'frozen Live stage identity')
        super().__init__(plan)
        for n,h in plan['tts_assets'].items():require(sha(n)==h,'native TTS asset drift')
        require(sha(VAD/WEIGHT)==DIGEST,'native VAD asset drift')
        require(sha(ROOT/plan['record']['path'])==plan['record']['sha256'],'project source drift')

    def extra_model_args(self):
        tts=self.plan['native_tts'];args=['--tts-model-dir',tts['directory'],
                                       '--vad-model-dir',self.plan['vad_directory']]
        for key,value in tts['limits'].items():args+=['--tts-'+key.replace('_','-'),str(value)]
        return args

    def query_audio(self):
        clip=retained_query()
        require(clip==self.plan['retained_query'],'retained audio/proof drift')
        path=self.out/'query.pcm';shutil.copyfile(clip['path'],path)
        require(sha(path)==clip['sha256'],'exact retained waveform')
        save(self.out/'query-fixture.json',dict(clip,path=str(path),
            reused=True,new_synthesis_work=0,original_phase_status='FAIL'))
        return dict(path=str(path),sha256=sha(path))

    def business(self):
        clip=self.query_audio()
        before=self.snapshot('before')
        self.budget('websocket_sessions');self.budget('audio_inputs');self.budget('responses',2)
        record=self.plan['record']
        args=dict(base_url=f"https://127.0.0.1:{self.plan['public_port']}/v1",
            api_key=self.keys['alpha'],ca_file=self.plan['tls']['certificate'],
            clip=clip['path'],clip_sha256=clip['sha256'],expected_input=QUERY,
            record_file=str(ROOT/record['path']),record_sha256=record['sha256'],
            expected_version=record['version'],journal_file=str(self.out/'client.events.jsonl'))
        env={k:v for k,v in os.environ.items() if k not in
             ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD','NODE_EXTRA_CA_CERTS')}
        env['Q4T_NODE_SDK_DIR']=self.plan['sdk_directory']
        with (self.out/'client.sdk.json').open('xb') as out,(self.out/'client.stderr').open('xb') as err:
            actor=subprocess.Popen(['node',str(ROOT/'tools/serve_live/client_sdk.mjs')],
                stdin=subprocess.PIPE,stdout=out,stderr=err,env=env)
            self.history[actor.pid]=base.lifecycle.guard.processes()[actor.pid]
            try:
                actor.stdin.write(json.dumps(args).encode());actor.stdin.close()
                code=actor.wait(timeout=315)
            finally:
                if actor.poll() is None:actor.terminate()
                try:actor.wait(5)
                except subprocess.TimeoutExpired:actor.kill();actor.wait(3)
        self.same_model();after=self.snapshot('live-drained')
        require((self.out/'client.sdk.json').stat().st_size>0,
                'actor did not publish its final receipt; retain raw journal and exit code '+str(code))
        receipt=read(self.out/'client.sdk.json')
        require(code==0 and receipt['status']=='PASS','actual SDK protocol failed; preserve original journal')
        journal=self.out/'client.events.jsonl'
        require(journal.stat().st_size<=8<<20,'journal bound')
        observation=checks.observe(receipt,[json.loads(line) for line in journal.read_text().splitlines()],
                                   before,after,record)
        save(self.out/'observation.json',observation)
        self.core('core-after');self.snapshot('final-drained')
        status,raw=self.http('/v1/config',label='final-config');require(status==200,'core diagnostics')
        save(self.out/'speech-health.json',checks.speech_health(json.loads(raw)))
        for p in (self.out/'client.sdk.json',journal):
            require(all(secret.encode() not in p.read_bytes() for secret in [self.token,*self.keys.values()]),
                    'private credential in public evidence candidate')


def finalize(out):
    plan=read(out/'plan.json');result=read(out/'result.json')
    require(result['status']=='PASS' and result['seconds']<=LIMITS['seconds'] and
            all(result['counts'][k]==v for k,v in LIMITS.items() if k not in ('seconds','controls')),
            'bounded actual work incomplete')
    for n,h in {**plan['sources'],**plan['protected']}.items():require(sha(ROOT/n)==h,'source/default drift')
    lease=out/'lease';cleanup=read(out/'cleanup.json')
    environment=qualification.environment(read(lease/'exit.json'),
        [json.loads(line) for line in (lease/'nvml-queries.jsonl').read_text().splitlines()], [cleanup], expected_starts=1)
    actual=checks.observe(read(out/'client.sdk.json'),
        [json.loads(line) for line in (out/'client.events.jsonl').read_text().splitlines()],
        read(out/'before.json'),read(out/'live-drained.json'),plan['record'])
    require(actual==read(out/'observation.json'),'independent final observation differs')
    qualification.background.probe_ports(tuple(plan[k] for k in ('port','operations_port','public_port')))
    with base.lifecycle.guard.common_lock().open('r+') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    save(out/'acceptance.json',dict(status='PASS_LIVE_CLIENT_PROTOCOL',environment=environment,
        observation=actual,counts=result['counts'],seconds=result['seconds'],
        quality='PASS' if all(q['status']=='PASS' for q in actual['quality']) else 'FAIL',
        default_unchanged=True,listener_validation=False,managed_recovery=False,master_complete=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('prepare','run','finalize'))
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();out=args.output.resolve()
    require(out.is_relative_to(WORK),'private Live evidence owner required')
    if args.mode=='prepare':prepare(out)
    elif args.mode=='finalize':finalize(out)
    else:
        with base.lifecycle.child_subreaper():Stage(read(out/'plan.json')).run()
