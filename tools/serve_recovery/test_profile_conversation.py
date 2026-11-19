"""Explicit CPU fixtures: cannot certify a real model or deployment."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_recovery import profile_acceptance as driver
from tools.serve_recovery import profile_conversation as conversation
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_migration as migration
from tools.serve_recovery import test_profile_migration as previous
from tools.serve_service import capacity


def declaration():
    return dict(implemented=True, endpoint='/v1/realtime',
        profile='q4t-native-realtime-chat-v1', requires_asr=False,
        output_modalities=['text'], function_calls=True,
        per_response_client_admission=True, usage='actual_tokens',
        automatic_replay=False, out_of_band=True, cancel=True,
        persistent_context_truncation=True, max_output_tokens=4096,
        empty_context='literal_instructions_ephemeral_model_turn',
        max_responses=4, default_conversation_writers=1, max_items=128,
        history_bytes=16 << 20, session_host_reservation_bytes=2 << 30,
        pending_execution=['audio', 'mcp', 'prompt', 'tracing'])


def record():
    source = dict(path='docs/STATUS.md', sha256='a'*64, version='rev346')
    tool = dict(record_key='release', version=source['version'],
                source=source['path'], source_sha256=source['sha256'])
    call = dict(id='cpu_call_item', type='function_call', status='completed',
        call_id='cpu_call', name='lookup_project_record',
        arguments='{"record_key":"release"}')
    answer = dict(id='cpu_answer_item', type='message', status='completed',
        role='assistant', content=[dict(type='output_text', text='rev346')])
    rows = [dict(id='cpu_response_'+str(i), status='completed', output=[item],
        conversation_id='cpu_conversation', output_modalities=['text'],
        usage=dict(input_tokens=100, output_tokens=10, total_tokens=110))
        for i,item in enumerate((call, answer))]
    events = []
    for i,row in enumerate(rows):
        if i:
            events.append(dict(type='conversation.item.done', item=dict(
                type='function_call_output', id='cpu_return',
                call_id=call['call_id'], output=json.dumps(tool))))
        events += [dict(type='response.created', response=dict(id=row['id'])),
            dict(type='response.output_text.delta' if i else
                      'response.function_call_arguments.delta',
                 response_id=row['id'], item_id=row['output'][0]['id'],
                 delta='rev346' if i else call['arguments']),
            dict(type='response.done', response=copy.deepcopy(row))]
    before = dict(admitted=3, settled=200, reserved=0, active=0, sessions=0,
                  host_bytes=0, free_slots=1)
    return dict(source=source, before=before,
        after=dict(before, admitted=5, settled=420), session=dict(
            status='PASS', mode='qualification', transport='official-openai-wss',
            connections=1, close_code=1000, automatic_replay=False,
            response_creates=2, tool_record=tool, responses=rows, events=events))


class ConversationEvidence(unittest.TestCase):
    def test_native_declaration_independent_of_asr_and_strictly_typed(self):
        caps = dict(realtime=dict(conversation=declaration()))
        self.assertTrue(evidence.conversation_enabled(caps))
        self.assertEqual(evidence.format_for({}, {}, caps), evidence.CONVERSATION_FORMAT)
        self.assertFalse(evidence.conversation_enabled({}))
        for field,value in [('requires_asr',True), ('implemented',1),
                ('automatic_replay',True), ('usage','estimated'),
                ('history_bytes',0), ('pending_execution',[])]:
            wrong = copy.deepcopy(caps); wrong['realtime']['conversation'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                evidence.conversation_enabled(wrong)
        with self.assertRaises(ValueError):
            evidence.conversation_enabled(dict(realtime=[]))

    def test_complete_source_bound_stream_and_settlement_are_not_modified(self):
        value = record(); original = copy.deepcopy(value)
        self.assertEqual(evidence.conversation_business(value), 220)
        self.assertEqual(value, original)

    def test_partial_replayed_or_failed_sdk_cannot_qualify(self):
        for field,value in [('status','FAIL'), ('mode','main'), ('connections',2),
                ('connections',True), ('automatic_replay',True),
                ('close_code',1006), ('response_creates',3)]:
            wrong = record(); wrong['session'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                evidence.conversation_business(wrong)
        wrong = record(); wrong['session']['events'].append(
            copy.deepcopy(wrong['session']['events'][-1]))
        with self.assertRaises(ValueError): evidence.conversation_business(wrong)

    def test_missing_foreign_reordered_or_changed_stream_cannot_qualify(self):
        for change in (lambda e:e.pop(1),
            lambda e:e[1].update(item_id='foreign'),
            lambda e:e[1].update(delta='{'),
            lambda e:e.append(e.pop(1)),
            lambda e:e.insert(0,e.pop(3)),
            lambda e:e.append(dict(type='error'))):
            wrong = record(); change(wrong['session']['events'])
            with self.assertRaises(ValueError): evidence.conversation_business(wrong)

    def test_changed_tool_provenance_and_foreign_result_cannot_qualify(self):
        for change in (lambda v:v['source'].update(path='/tmp/fake'),
            lambda v:v['source'].update(sha256='bad'),
            lambda v:v['session']['tool_record'].update(version='rev0'),
            lambda v:v['session']['events'][3]['item'].update(call_id='foreign'),
            lambda v:v['session']['events'][3]['item'].update(output='{}')):
            wrong = record(); change(wrong)
            with self.assertRaises(ValueError): evidence.conversation_business(wrong)

    def test_changed_answer_or_unknown_usage_cannot_qualify(self):
        for change in (lambda r:r.update(status='incomplete'),
            lambda r:r.update(conversation_id='foreign'),
            lambda r:r.update(output_modalities=['audio']),
            lambda r:r['output'][0]['content'][0].update(text='rev0'),
            lambda r:r['usage'].update(total_tokens=111),
            lambda r:r['usage'].update(output_tokens=True)):
            wrong = record(); change(wrong['session']['responses'][1])
            wrong['session']['events'][-1]['response'] = copy.deepcopy(
                wrong['session']['responses'][1])
            with self.assertRaises(ValueError): evidence.conversation_business(wrong)

    def test_unknown_or_leaked_resources_and_false_settlement_cannot_qualify(self):
        for field,value in [('admitted',6), ('settled',419), ('reserved',1),
                ('active',1), ('sessions',1), ('host_bytes',True), ('free_slots',0)]:
            wrong = record(); wrong['after'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                evidence.conversation_business(wrong)
        wrong = record(); wrong['after'].pop('host_bytes')
        with self.assertRaises(ValueError): evidence.conversation_business(wrong)


class ConversationDriver(unittest.TestCase):
    def test_exact_budget_without_asr_and_composed_with_semantic(self):
        profile = capacity.normalize(dict(version=1))
        plain = driver.limits_for(profile)
        selected = driver.limits_for(profile, conversation=True)
        self.assertEqual(selected['chats']-plain['chats'],4)
        self.assertEqual((selected['websocket_sessions'],selected['ingress_starts']), (2,2))
        asr = dict(vad={})
        before = driver.limits_for(profile,native_asr=asr,realtime=True,semantic_vad=True)
        after = driver.limits_for(profile,native_asr=asr,realtime=True,
                                 semantic_vad=True,conversation=True)
        self.assertEqual(after['chats']-before['chats'],4)
        self.assertEqual((after['websocket_sessions'],after['ingress_starts']), (8,6))
        with self.assertRaises(RuntimeError): driver.limits_for(profile,conversation=1)

    def test_cli_preserves_explicit_conversation_scope(self):
        with mock.patch.object(driver,'prepare') as prepare, mock.patch('sys.argv',[
            'profile_acceptance.py','prepare','--binary','/bin','--package','/pkg',
            '--output','/output','--conversation-reference','/reference']):
            driver.main()
        self.assertEqual(prepare.call_args.args[0].conversation_reference,Path('/reference'))

    def test_sdk_timeout_cleans_owned_entry_without_false_business(self):
        parent = conversation.ROOT/'.q4t-work/conversation-profile-cpu'
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as tmp:
            out = Path(tmp); charged = []
            plan = dict(format='q4t-profile-conversation-inputs-v1', files={},
                sdk_directory='/sdk', ingress='/entry', public_port=3,
                tls=dict(certificate='/ca'), source=record()['source'])
            stage = SimpleNamespace(out=out, profile_name='primary', history={},
                budget=charged.append, model=SimpleNamespace(poll=lambda:None),
                http=lambda *a,**k:(200,b''), plan=dict(conversation=plan,
                    port=1, operations_port=2,
                    sdk=dict(sdk_directory='/sdk',node=dict(path='/node'))))
            conversation.save(out/'client-policy.json',dict(gateway_token='cpu',
                clients=[dict(id=conversation.realtime.encrypted.CLIENT)]))
            edge = SimpleNamespace(process=SimpleNamespace(pid=99,returncode=0),
                                   close=mock.Mock())
            with (mock.patch.object(conversation,'NativeIngress',return_value=edge),
                  mock.patch.object(conversation.realtime,'state',return_value=record()['before']),
                  mock.patch.object(conversation.realtime.encrypted.foreground.base.lifecycle.guard,
                                    'processes',return_value={99:{}}),
                  mock.patch.object(conversation.subprocess,'run',
                      side_effect=subprocess.TimeoutExpired('sdk',255))):
                with self.assertRaises(subprocess.TimeoutExpired):conversation.run(stage)
            edge.close.assert_called_once()
            self.assertEqual(charged,['ingress_starts','websocket_sessions','chats','chats'])
            self.assertTrue((out/'primary-conversation-entry-cleanup.json').exists())
            self.assertFalse((out/'primary-conversation-business.json').exists())


class ConversationMigration(unittest.TestCase):
    setUp = previous.MigrationTests.setUp
    stamp = previous.MigrationTests.stamp

    def pair(self):
        rows = {n:copy.deepcopy(row['record']) for n,row in self.first.items()}
        for row in rows.values():
            row['capabilities']['realtime'] = dict(
                transcription_configured=False, automatic_replay=False,
                per_turn_client_admission=True)
        first = previous.bind_records(self.old, rows)
        rows = copy.deepcopy(rows)
        for row in rows.values():
            row['format'] = evidence.CONVERSATION_FORMAT
            row['capabilities']['realtime']['conversation'] = declaration()
            row['business']['conversation'] = record()
        return first, previous.bind_records(self.new, rows)

    def test_old_or_missing_receipt_cannot_claim_new_capability(self):
        _, second = self.pair()
        rows = {n:copy.deepcopy(row['record']) for n,row in second.items()}
        for mutation in (lambda r:r['business'].pop('conversation'),
                         lambda r:r.update(format='q4t-profile-business-v16')):
            wrong = copy.deepcopy(rows); mutation(wrong['primary'])
            fixed = copy.deepcopy(self.new); bound = previous.bind_records(fixed,wrong)
            with self.assertRaises(ValueError):
                evidence.validate(bound['primary']['record'],fixed,'primary',
                                  bound['primary']['sha256'])

    def test_manual_extension_and_automatic_recovery_keep_actual_capabilities(self):
        first, second = self.pair(); original = copy.deepcopy((first,second))
        grant = migration.permit(self.old,self.new,first,second,
            self.config,self.config,allow_capability_extension=True)
        self.assertFalse(grant['automatic'])
        with self.assertRaisesRegex(ValueError,'capabilities'):
            migration.permit(self.new,self.old,second,first,
                             self.config,self.config,automatic=True)
        same_registry = copy.deepcopy(self.old)
        same = previous.bind_records(same_registry,
            {n:copy.deepcopy(r['record']) for n,r in second.items()})
        self.assertTrue(migration.permit(self.new,same_registry,second,same,
            self.config,self.config,automatic=True)['automatic'])
        self.assertEqual((first,second),original)


if __name__ == '__main__': unittest.main()
