"""Synthetic recovery negatives, never real model or service qualification."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools.serve_realtime import conversation_managed as m
from tools.serve_realtime.test_managed import state, wire
from tools.serve_recovery.test_profile_conversation import record, declaration
from tools.native_asr import service_acceptance as service
from tools.serve_service.package import read, save


def session(mode='complete', transport='official-openai-wss', prefix='one'):
    value = record()['session']
    def ids(v):
        if isinstance(v,list):
            for item in v: ids(item)
        elif isinstance(v,dict):
            for k,x in v.items():
                if k in ('id','response_id','item_id','call_id','conversation_id') and isinstance(x,str):
                    v[k] = prefix+'-'+x
                else: ids(x)
    ids(value)
    value.update(phase='result',mode=mode,transport=transport,
        close_code=1000 if mode == 'complete' else 1006,partial=None)
    if mode == 'hold_stream':
        value['response_creates'] = 3
        value['partial'] = dict(response_id=prefix+'-pending',item_id=prefix+'-partial',delta='1')
        value['events'] += [dict(type='response.created',response=dict(
            id=prefix+'-pending',conversation_id=None)),
            dict(type='response.output_text.delta',**value['partial'])]
    return value


def core(index=0):
    return dict(business_ready=True,model_pid=12+index,model_start_ticks=30+index,
                model_generation=1+index,controller_pid=10+index,controller_start_ticks=20+index)


class Evidence(unittest.TestCase):
    def setUp(self):
        parent=Path(os.environ['Q4T_TEST_OUTPUT_DIR'])
        temp=tempfile.TemporaryDirectory(dir=parent);self.addCleanup(temp.cleanup)
        self.out=Path(temp.name)

    def test_complete_and_interrupted_receipts_keep_original_events(self):
        for mode in ('complete','hold_stream','hold_complete'):
            raw=session(mode);original=copy.deepcopy(raw)
            self.assertEqual(m.check_session(raw,mode,'official-openai-wss',record()['source']),220)
            self.assertEqual(raw,original)

    def test_transport_replay_missing_partial_and_false_terminal_reject(self):
        for change in (lambda v:v.update(connections=2),
                lambda v:v.update(automatic_replay=True),lambda v:v.update(close_code=1000),
                lambda v:v.update(transport='ws-loopback'),lambda v:v.update(partial=None),
                lambda v:v['partial'].update(item_id='foreign'),
                lambda v:v['events'][-2]['response'].update(conversation_id='default'),
                lambda v:v['events'].append(dict(type='response.done',response=dict(id='one-pending')))):
            raw=session('hold_stream');change(raw)
            with self.assertRaises((ValueError,RuntimeError)):
                m.check_session(raw,'hold_stream','official-openai-wss',record()['source'])

    def test_actual_usage_uses_server_settlement_without_inventing_partial_tokens(self):
        before=state();after=state(admitted=3,total=3,success=2,aborted=1,settled=250)
        self.assertEqual(m.accounting(before,after,220,True),250)
        self.assertEqual(m.accounting(before,state(admitted=2,total=2,success=2,settled=220),220),220)
        for change in (dict(settled=220),dict(admitted=4),dict(success=3),
                       dict(aborted=0),dict(errors=1),dict(total=2)):
            with self.assertRaises(RuntimeError):m.accounting(before,dict(after,**change),220,True)

    def fixture(self,scenario='tls'):
        self.plan=dict(scenario=scenario,conversation=dict(format='q4t-managed-conversation-v1',
            source=record()['source']),limits=dict(websocket_sessions=1 if scenario=='tool_retained' else 2))
        self.caps=dict(realtime=dict(conversation=declaration()))
        previous=None
        for index,(label,mode) in enumerate(m.phases(self.plan)):
            prefix=label+'-conversation';tls=scenario=='tls'
            raw=session(mode,'official-openai-wss' if tls else 'ws-loopback',str(index))
            before=previous if previous is not None and tls else state()
            after=dict(before,admitted=before['admitted']+(3 if mode=='hold_stream' else 2),
                total=before['total']+(3 if mode=='hold_stream' else 2),
                success=before['success']+2,aborted=before['aborted']+(mode=='hold_stream'),
                settled=before['settled']+(250 if mode=='hold_stream' else 220))
            previous=after
            row=dict(mode=mode,before=before,after=None if mode=='hold_complete' else after)
            if mode!='complete':
                held=dict(after,aborted=before['aborted'],settled=before['settled']+220,
                          sessions=1,host_bytes=2<<30,active=1 if mode=='hold_stream' else 0,
                          reserved=200 if mode=='hold_stream' else 0)
                row['held']=held
                save(self.out/(prefix+'-ready.json'),dict(phase='ready',mode=mode,
                    transport=raw['transport'],connections=1,response_creates=raw['response_creates'],
                    partial=raw['partial'],completed_ids=[r['id'] for r in raw['responses']]))
            save(self.out/(prefix+'.sdk.json'),raw);save(self.out/(prefix+'-receipt.json'),row)
            for suffix in ('before','after','held'):
                if row.get(suffix) is not None:
                    save(self.out/(prefix+'-'+suffix+'-metrics.json'),
                         [dict(http_status=200,body=wire(row[suffix]))])
            for suffix in ('before','after','held'):
                save(self.out/(prefix+'-core-'+suffix+'.json'),core(index if scenario=='tool_version' else 0))
        save(self.out/'counts.json',dict(counts=self.plan['limits']))

    def test_raw_metrics_owner_and_budget_required_for_every_phase(self):
        for scenario in ('tls','tool_version','tool_retained'):
            self.fixture(scenario)
            value=m.check(self.out,self.plan,{},self.caps)
            self.assertEqual(len(value),1 if scenario=='tool_retained' else 2)
        for mutate in (lambda r:r['after'].update(sessions=1),
                       lambda r:r['before'].update(settled=1)):
            self.fixture('tls');path=self.out/'tls-after-conversation-receipt.json'
            row=read(path);mutate(row);save(path,row)
            with self.assertRaises(RuntimeError):m.check(self.out,self.plan,{},self.caps)

    def test_held_connection_and_core_identity_cannot_be_faked(self):
        for field,value in [('connections',2),('partial',None),('completed_ids',[])]:
            self.fixture();path=self.out/'tls-before-conversation-ready.json'
            row=read(path);row[field]=value;save(path,row)
            with self.assertRaises(RuntimeError):m.check(self.out,self.plan,{},self.caps)
        self.fixture();path=self.out/'tls-after-conversation-core-after.json'
        save(path,core(1))
        with self.assertRaises(RuntimeError):m.check(self.out,self.plan,{},self.caps)

    def test_disabled_or_missing_execution_is_not_ignored(self):
        self.assertEqual(m.check(self.out,{}, {}, {}),{})
        with self.assertRaises(RuntimeError):m.check(self.out,{}, {},dict(realtime=dict(conversation=declaration())))
        self.fixture();save(self.out/'counts.json',dict(counts=dict(websocket_sessions=1)))
        with self.assertRaises(RuntimeError):m.check(self.out,self.plan,{},self.caps)

    def test_exact_composed_budgets_and_single_fault_owner(self):
        for scenario,count in [('tool_retained',1),('tls',2),('tool_version',2)]:
            flags=dict(chat_audio=True,native_tts=True,native_voice=True,realtime=True,semantic_vad=True)
            before=service.limits_for(scenario,**flags)
            after=service.limits_for(scenario,conversation=True,**flags)
            self.assertEqual(after['starts'],before['starts'])
            self.assertEqual(after['websocket_sessions']-before['websocket_sessions'],count)
            self.assertEqual(after['chats']-before['chats'],2*count+int(scenario=='tls'))
            plan=dict(scenario=scenario,conversation={})
            self.assertTrue(all(mode=='complete' for _,mode in m.managed.phases(plan)))
            self.assertEqual(m.phases(plan),m.managed.recovery_phases(plan))
        with self.assertRaises(RuntimeError):service.limits_for('tls_remaining',conversation=True)

    def test_asr_chat_fault_handoff_requires_exact_counter_continuity(self):
        import hashlib
        from tools.serve_realtime import test_managed as original
        plan=dict(scenario='tls',conversation={},realtime=dict(format='q4t-managed-realtime-v1'),
                  limits=dict(websocket_sessions=4))
        first=state();previous=None
        for index,label in enumerate(('tls-before','tls-after')):
            before=first if index==0 else state(admitted=4,total=4,success=3,aborted=1,settled=331)
            after=dict(before,admitted=before['admitted']+1,total=before['total']+1,
                       success=before['success']+1,settled=before['settled']+81)
            row=dict(mode='complete',before=before,after=after)
            save(self.out/(label+'-realtime-receipt.json'),row)
            save(self.out/(label+'-realtime.sdk.json'),original.session(
                'complete','openai-realtime-wss','asr-'+str(index)))
            for suffix in ('before','after'):
                save(self.out/(label+'-realtime-'+suffix+'-metrics.json'),
                     [dict(http_status=200,body=wire(row[suffix]))])
            if index==0:previous=after
            else:save(self.out/'tls-before-conversation-receipt.json',dict(before=previous,after=before))
        save(self.out/'counts.json',dict(counts=plan['limits']))
        caps=dict(realtime=dict(transcription_configured=True,automatic_replay=False,per_turn_client_admission=True))
        with mock.patch.object(m.evidence,'REALTIME_WAVEFORM_SHA',hashlib.sha256(original.WAVE).hexdigest()):
            self.assertEqual(len(m.managed.check(self.out,plan,dict(native_asr={}),caps)),2)
            path=self.out/'tls-before-conversation-receipt.json';row=read(path)
            row['after']['settled']+=1;save(path,row)
            with self.assertRaises(RuntimeError):m.managed.check(self.out,plan,dict(native_asr={}),caps)

    def test_pinned_production_namespace_contains_only_pure_shared_verifier(self):
        from tools.serve_recovery import bundle
        source=self.out/'sealed/source'
        for name in bundle.SOURCES:
            target=source/name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(m.managed.ROOT/name,target)
        save(self.out/'raw.json',session());save(self.out/'source.json',record()['source'])
        script='''import json,runpy,sys
from pathlib import Path
root,sealed,raw,source=map(Path,sys.argv[1:]);sys.path.insert(0,str(root))
driver=runpy.run_path(str(root/'tools/serve_recovery/acceptance.py'))
driver['pin_packaged_modules'](sealed)
from tools.serve_realtime import conversation_managed as m
assert Path(m.evidence.__file__).resolve().is_relative_to(sealed)
assert m.check_session(json.loads(raw.read_text()),'complete','official-openai-wss',json.loads(source.read_text()))==220
print('PASS pinned shared verifier')
'''
        run=subprocess.run([sys.executable,'-I','-B','-c',script,str(m.managed.ROOT),
            str(source),str(self.out/'raw.json'),str(self.out/'source.json')],
            capture_output=True,text=True,timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertIn('PASS pinned',run.stdout)


if __name__ == '__main__':unittest.main()
