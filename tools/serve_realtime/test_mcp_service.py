"""Recovery checker counterexamples; CPU fixtures do not qualify a model."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.serve_realtime import mcp_managed as m, managed
from tools.serve_realtime import mcp_recovery_checks as c
from tools.serve_realtime import mcp_service_acceptance as service
from tools.serve_recovery.test_profile_mcp import actual


def fixture(mode='complete', history=None):
    data = actual('light')
    original = data['original']['session']
    source = data['source']
    row = copy.deepcopy(original['responses'][0])
    call = next(i for i in row['output'] if i['type'] == 'mcp_call')
    tool = copy.deepcopy(original['tools'][0])
    approval = next(e['item'] for e in original['events'] if e['type'] ==
        'conversation.item.done' and e.get('item', {}).get('type') == 'mcp_approval_request')
    text = copy.deepcopy(original['responses'][1])
    if mode in ('policy', 'hold_policy'):
        text['output'][0]['content'][0]['text'] = '42'
    def user(identifier, prompt):
        return dict(type='conversation.item.create', item=dict(id=identifier,
            type='message', role='user', content=[dict(type='input_text', text=prompt)]))
    definition = dict(type='mcp', server_label='project', server_url='https://localhost:1234/mcp',
        allowed_tools=['read_project_record'], require_approval='always', authorization='<redacted>')
    history = history or []
    client = [dict(type='conversation.item.create', item=i) for i in history]
    client += [dict(type='session.update', session=dict(type='realtime', tools=[definition]))]
    events = [dict(type='session.created'), dict(type='conversation.created')]
    events += [dict(type='conversation.item.done', item=i) for i in history]
    events += [dict(type='session.updated', session=dict(type='realtime', tools=[dict(
        (k,v) for k,v in definition.items() if k != 'authorization')]))]
    policy = mode in ('policy', 'hold_policy')
    events += [dict(type='mcp_list_tools.failed' if policy else 'mcp_list_tools.completed')]
    responses, tools = [], []
    prompts = [(text, 'user_mcp_after_fault', c.c.ORDINARY_PROMPT)] if policy else [
        (row, 'user_mcp_read', c.c.READ_PROMPT)] + ([] if mode == 'hold_approval' else [
            (text, 'user_mcp_follow', c.FOLLOW_PROMPT)])
    for response, identifier, prompt in prompts:
        message = user(identifier, prompt)
        client.append(message)
        events.append(dict(type='conversation.item.done', item=message['item']))
        request = dict(conversation='auto', output_modalities=['text'], max_output_tokens=256)
        is_call = response is row
        request.update(dict(tool_choice=dict(type='mcp', server_label='project',
            name='read_project_record')) if is_call else dict(tools=[], tool_choice='none'))
        client.append(dict(type='response.create', response=request))
        events += [dict(type='response.created', response=dict(id=response['id']))]
        if is_call:
            events.append(dict(type='response.mcp_call_arguments.delta', response_id=row['id'],
                               item_id=call['id'], delta=call['arguments']))
        else:
            events.append(dict(type='response.output_text.delta', response_id=text['id'],
                item_id=text['output'][0]['id'], output_index=0, content_index=0,
                delta=text['output'][0]['content'][0]['text']))
        events.append(dict(type='response.done', response=response))
        responses.append(response)
        if is_call:
            events.append(dict(type='conversation.item.done', item=approval))
            if mode != 'hold_approval':
                decision = dict(type='mcp_approval_response', id='decision_'+call['id'],
                                approval_request_id=approval['id'], approve=True)
                client.append(dict(type='conversation.item.create', item=decision))
                events += [dict(type='conversation.item.done', item=decision),
                    dict(type='response.mcp_call.in_progress', response_id=row['id'], item_id=call['id']),
                    dict(type='response.output_item.done', response_id=row['id'], output_index=0, item=tool),
                    dict(type='conversation.item.done', item=tool)]
                tools.append(tool)
    for i, event in enumerate(events): event['event_id'] = 'cpu-'+str(i)
    value = dict(phase='result', status='PASS', mode=mode, transport='official-openai-wss',
        connections=1, automatic_replay=False, response_creates=c.work(mode),
        close_code=1000 if mode in ('complete', 'policy') else 1006,
        events=events, responses=responses, tools=tools, restored=history,
        approval=None if policy else approval, sent_events=len(client),
        event_bytes=sum(len(json.dumps(e).encode()) for e in events), transport_errors=[],
        client_journal_authorization='redacted')
    journal = [dict(direction=direction, event=e, monotonic_ns=str(i))
        for direction, rows in [('client',client),('server',events)] for i,e in enumerate(rows)]
    return value, journal, source


class Contracts(unittest.TestCase):
    def check(self, triple, mode='complete', history=None):
        value, journal, source = triple
        return c.check_session(value, mode, 'official-openai-wss', journal, source, history or [])

    def test_all_modes_count_generated_work_and_real_reads(self):
        for mode in c.MODES:
            row = self.check(fixture(mode), mode)
            self.assertEqual(row['jobs'], c.work(mode))
            self.assertEqual(row['actual_reads'], int(mode in ('complete', 'hold_complete')))

    def test_pending_approval_may_not_dispatch_or_approve(self):
        for kind in ('response.mcp_call.in_progress', 'conversation.item.done'):
            value, journal, source = fixture('hold_approval')
            event = dict(type=kind, event_id='extra', item=dict(type='mcp_approval_response'))
            value['events'].append(event)
            journal.append(dict(direction='server', event=event, monotonic_ns='100'))
            with self.assertRaises(RuntimeError): self.check((value,journal,source), 'hold_approval')

    def test_approval_order_and_argument_stream_are_bound(self):
        for mutation in ('order', 'argument', 'owner'):
            value,journal,source = fixture()
            if mutation == 'argument':
                next(e for e in value['events'] if e['type'] == 'response.mcp_call_arguments.delta')['delta'] = '{}'
            elif mutation == 'owner': value['tools'][0]['approval_request_id'] = 'foreign'
            else:
                events = value['events']
                sent = next(e for e in events if e['type'] == 'response.mcp_call.in_progress')
                events.remove(sent); events.insert(1,sent)
                journal = [r for r in journal if r['direction'] == 'client'] + [dict(
                    direction='server',event=e,monotonic_ns=str(i)) for i,e in enumerate(events)]
            with self.assertRaises(RuntimeError): self.check((value,journal,source))

    def test_exact_completed_history_is_authenticated(self):
        history = [copy.deepcopy(fixture()[0]['tools'][0])]
        history[0]['id'] = 'prior-call'
        triple = fixture(history=history)
        self.assertEqual(self.check(triple,history=history)['history_authenticated'],1)
        triple[0]['restored'][0] = dict(history[0], output='forged')
        with self.assertRaises(RuntimeError): self.check(triple,history=history)

    def test_quality_failure_remains_separate_but_stream_mismatch_fails(self):
        value,journal,source = fixture()
        response = value['responses'][-1]
        response['output'][0]['content'][0]['text'] = 'wrong answer'
        with self.assertRaises(RuntimeError): self.check((value,journal,source))
        next(e for e in value['events'] if e['type'] == 'response.output_text.delta')['delta'] = 'wrong answer'
        observed = self.check((value,journal,source))
        self.assertEqual(observed['model_quality_status'],'FAIL')
        self.assertEqual(observed['quality'][0]['answer'],'wrong answer')

    def test_public_journal_must_redact_credentials(self):
        value,journal,source = fixture()
        next(r['event'] for r in journal if r['direction'] == 'client' and
            r['event']['type'] == 'session.update')['session']['tools'][0]['authorization'] = 'secret'
        with self.assertRaises(RuntimeError): self.check((value,journal,source))

    def test_actual_peer_replay_and_wrong_source_rejected(self):
        observed=self.check(fixture());source=fixture()[2]
        before=dict(calls=[],methods=[])
        after=dict(calls=[dict(ok=True,source=source)],methods=['tools/call'])
        c.peer(before,after,observed,source)
        for changed in (dict(after,calls=after['calls']*2),
                        dict(after,methods=['tools/call']*2),
                        dict(after,calls=[dict(ok=True,source={})])):
            with self.assertRaises(RuntimeError):c.peer(before,changed,observed,source)

    def test_settlement_cannot_hide_replay_or_missing_usage(self):
        observed=self.check(fixture())
        before={k:0 for k in managed.METRICS};before['free_slots']=1
        after=dict(before,admitted=2,total=2,success=2,settled=observed['tokens'])
        c.accounting(before,after,observed)
        for changed in (dict(settled=0),dict(admitted=3),dict(errors=1),dict(success=1)):
            with self.assertRaises(RuntimeError):c.accounting(before,dict(after,**changed),observed)

    def test_frozen_group_includes_default_and_configured_packages(self):
        rows=list(service.CONFIGURED_LIMITS.values())+list(service.DEFAULT_LIMITS.values())
        self.assertEqual([sum(r[k] for r in rows) for k in
            ('starts','chats','websocket_sessions','probes','seconds')],[8,22,9,2,1500])
        plan=dict(scenario='tls',mcp_recovery=dict(format=m.FORMAT,default_policy=False))
        self.assertEqual(m.phases(plan)[0],('tls-before','hold_approval'))
        with self.assertRaises(RuntimeError):m.phases(dict(plan,mcp_recovery=dict(format=m.FORMAT,default_policy=True)))

    def test_verification_failure_still_preserves_actual_settlement(self):
        root=Path(os.environ['Q4T_TEST_OUTPUT_DIR'])
        with tempfile.TemporaryDirectory(dir=root) as directory:
            stage=mock.Mock(output=Path(directory),plan=dict(scenario='tool_retained',
                mcp_recovery=dict(source=fixture()[2])))
            stage.sample.return_value=dict(model_pid=123)
            stage.peer.snapshot.return_value=dict(calls=[])
            actor=m.Actor(stage);actor.current=dict(prefix='failed',mode='complete')
            actor.process=mock.Mock(returncode=0);actor.process.stdout.read.return_value=b''
            actor.stderr=mock.Mock();actor.line=mock.Mock(return_value=fixture()[0])
            actor.observe=mock.Mock(return_value=dict(actual_settled=30))
            with mock.patch.object(m,'journal',return_value=[]),mock.patch.object(
                c,'check_session',side_effect=RuntimeError('original check failed')):
                with self.assertRaisesRegex(RuntimeError,'original check failed'):actor.finish()
            receipt=json.loads((Path(directory)/'failed-receipt.json').read_text())
            self.assertEqual(receipt['after'],dict(actual_settled=30))
            self.assertEqual(receipt['verification_failure'],'original check failed')


if __name__=='__main__':unittest.main()
