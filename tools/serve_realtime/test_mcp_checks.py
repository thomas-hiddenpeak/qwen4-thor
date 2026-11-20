"""Counterexamples prevent fixtures from certifying unobserved MCP business."""
import copy
import json
import unittest
from tools.serve_realtime import mcp_checks as c


def fixture(mode=c.MODES[0]):
    source=dict(path='docs/MODEL.md',sha256='a'*64,citation='[docs/MODEL.md:L1-L3]',
                text='1: # Actual title\n2: \n3: Note',start_line=1,end_line=3,truncated=False)
    record=dict(ok=True,source=copy.deepcopy(source),total_lines=20)
    call=lambda n:dict(id='call_'+str(n),type='mcp_call',server_label='project',
                       name='read_project_record',arguments=json.dumps(c.peer.ARGUMENTS))
    message=lambda value:dict(type='message',content=[dict(type='output_text',text=value)])
    rows=[dict(id='response_'+str(n),status='completed',usage=dict(input_tokens=12,
        output_tokens=2,total_tokens=14),output=[call(n) if n in (0,2) else
        message('Actual title [docs/MODEL.md:L1-L3]' if n==1 else '42')]) for n in range(4)]
    actual=[dict(call(0),output=json.dumps(dict(structuredContent=record,isError=False))),
            dict(call(2),error=dict(type='tool_execution_error',message=
                'mcp_approval_denied' if mode==c.MODES[0] else 'mcp_result_unknown'))]
    target=actual[1 if mode==c.MODES[0] else 0];target['approval_request_id']='approval'
    proposal=dict(type='mcp_approval_request',id='approval',name=target['name'],
                  server_label=target['server_label'],arguments=target['arguments'])
    decision=dict(type='mcp_approval_response',id='decision',approval_request_id='approval',
                  approve=mode==c.MODES[1])
    events=[]
    for n,row in enumerate(rows):
        if n==3:
            events.append(dict(type='conversation.item.done',item=dict(type='message',
                id='user_mcp_after_fault',role='user',
                content=[dict(type='input_text',text=c.ORDINARY_PROMPT)])))
        events.append(dict(type='response.created',response=dict(id=row['id'])))
        if n in (0,2):
            events.append(dict(type='response.mcp_call_arguments.delta',response_id=row['id'],
                item_id=row['output'][0]['id'],delta=row['output'][0]['arguments']))
        events.append(dict(type='response.done',response=copy.deepcopy(row)))
        if n in (0,2):
            tool=actual[n//2]
            if tool['id']==target['id']:
                events.extend([dict(type='conversation.item.done',item=proposal),
                               dict(type='conversation.item.done',item=decision)])
                if mode==c.MODES[1]:
                    events.append(dict(type='response.mcp_call.in_progress',item_id=tool['id']))
            events.extend([dict(type='response.output_item.done',item=copy.deepcopy(tool)),
                           dict(type='conversation.item.done',item=copy.deepcopy(tool))])
    if mode==c.MODES[0]:events.append(dict(type='mcp_list_tools.failed',item_id='broken'))
    receipt=dict(status='PASS',mode=mode,transport='official-openai-wss',connections=1,
        close_code=1000,automatic_replay=False,responses=rows,tools=actual,events=events,
        quality=[dict(case='document',answer='Actual title [docs/MODEL.md:L1-L3]',status='PASS'),
                 dict(case='ordinary_after_fault',answer='42',status='PASS')])
    remote=dict(calls=[copy.deepcopy(record) for _ in range(1 if mode==c.MODES[0] else 2)],
                methods=['tools/call']*(1 if mode==c.MODES[0] else 2))
    before=dict(admitted=0,settled=0,reserved=0,active=0,sessions=0,host_bytes=0,free_slots=1)
    after=dict(before,admitted=4,settled=56)
    return [receipt,copy.deepcopy(events),remote,source,before,after]


def reject(test,value):
    value[1]=copy.deepcopy(value[0]['events'])
    with test.assertRaises(Exception):c.observe(*value)


class McpEvidence(unittest.TestCase):
    def test_both_actual_business_shapes(self):
        for mode in c.MODES:
            self.assertEqual(c.observe(*fixture(mode))['settled_tokens'],56)

    def test_reconnect_or_automatic_replay_rejected(self):
        for field,value in [('connections',2),('close_code',1006),('automatic_replay',True)]:
            sample=fixture();sample[0][field]=value;reject(self,sample)

    def test_false_usage_and_unreleased_resources_rejected(self):
        for key in ('admitted','settled','reserved','active','sessions','host_bytes','free_slots'):
            sample=fixture();sample[5][key]+=1;reject(self,sample)

    def test_wrong_call_and_arguments_rejected(self):
        for key,value in [('name','fake'),('id','foreign'),('arguments','{}')]:
            sample=fixture();sample[0]['tools'][0][key]=value;reject(self,sample)

    def test_generation_cannot_claim_tool_completion(self):
        sample=fixture();sample[0]['responses'][0]['output'][0]['output']='invented'
        sample[0]['events'][2]['response']=copy.deepcopy(sample[0]['responses'][0]);reject(self,sample)
        sample=fixture();events=sample[0]['events'];events[2],events[3]=events[3],events[2]
        reject(self,sample)

    def test_duplicate_terminal_and_different_journal_rejected(self):
        sample=fixture();sample[0]['events'].append(copy.deepcopy(sample[0]['events'][3]));reject(self,sample)
        sample=fixture();sample[1].pop()
        with self.assertRaises(Exception):c.observe(*sample)

    def test_stream_or_durable_source_forgery_rejected(self):
        sample=fixture();sample[0]['events'][1]['delta']='{}';reject(self,sample)
        sample=fixture();sample[3]['sha256']='b'*64;reject(self,sample)
        sample=fixture();sample[2]['calls'].append(copy.deepcopy(sample[2]['calls'][0]));reject(self,sample)

    def test_unknown_outcome_and_denial_cannot_be_success(self):
        for mode in c.MODES:
            sample=fixture(mode);sample[0]['tools'][1]['error']['message']='invented';reject(self,sample)
        sample=fixture();sample[0]['events']=[e for e in sample[0]['events'] if e['type']!='mcp_list_tools.failed']
        reject(self,sample)

    def test_approval_is_owned_and_precedes_dispatch(self):
        sample=fixture(c.MODES[1]);sample[0]['tools'][0]['approval_request_id']='foreign';reject(self,sample)
        sample=fixture(c.MODES[1]);events=sample[0]['events'];sent=next(e for e in events if e['type']=='response.mcp_call.in_progress')
        events.remove(sent);events.insert(0,sent);reject(self,sample)
        sample=fixture();sample[0]['events'].append(dict(type='response.mcp_call.in_progress',item_id='call_2'));reject(self,sample)

    def test_model_quality_failure_retained_separately(self):
        sample=fixture();row=sample[0]['responses'][1];row['output'][0]['content'][0]['text']='wrong'
        for e in sample[0]['events']:
            if e['type']=='response.done' and e['response']['id']==row['id']:e['response']=copy.deepcopy(row)
        sample[0]['quality'][0].update(answer='wrong',status='FAIL');sample[0]['status']='QUALITY_FAIL'
        sample[1]=copy.deepcopy(sample[0]['events'])
        observed=c.observe(*sample);self.assertEqual(observed['protocol'],'PASS');self.assertEqual(observed['quality'][0]['status'],'FAIL')
        sample[0]['status']='PASS';reject(self,sample)

    def test_new_question_is_an_actual_user_turn_before_admission(self):
        for change in ('missing','late','content'):
            sample=fixture();events=sample[0]['events'];question=next(e for e in events if
                e.get('item',{}).get('id')=='user_mcp_after_fault')
            if change=='content':question['item']['content'][0]['text']='irrelevant'
            else:
                events.remove(question)
                if change=='late':events.append(question)
            reject(self,sample)

    def test_budget_terminal_is_protocol_valid_but_quality_incomplete(self):
        sample=fixture();row=sample[0]['responses'][3];row['status']='incomplete'
        row['status_details']=dict(reason='max_output_tokens')
        row['usage']=dict(input_tokens=12,output_tokens=256,total_tokens=268)
        row['output'][0]['content'][0]['text']='42'
        for e in sample[0]['events']:
            if e['type']=='response.done' and e['response']['id']==row['id']:e['response']=copy.deepcopy(row)
        sample[0]['quality'][1]['status']='FAIL';sample[0]['status']='QUALITY_FAIL'
        sample[5]['settled']=310;sample[1]=copy.deepcopy(sample[0]['events'])
        observed=c.observe(*sample);self.assertEqual(observed['protocol'],'PASS')
        self.assertEqual(observed['quality'][1]['status'],'FAIL')
        sample[0]['status']='PASS';reject(self,sample)


class RetainedRepair(unittest.TestCase):
    @staticmethod
    def sample():
        original=fixture(); receipt,events,remote,source,before,after=original
        retained=copy.deepcopy(receipt['tools']);row=receipt['responses'][-1]
        events=[dict(type='conversation.item.done',item=copy.deepcopy(i)) for i in retained]+[
            dict(type='mcp_list_tools.failed',item_id='broken'),
            dict(type='conversation.item.done',item=dict(type='message',id='user_mcp_after_fault',
                role='user',content=[dict(type='input_text',text=c.ORDINARY_PROMPT)])),
            dict(type='response.created',response=dict(id=row['id'])),
            dict(type='response.done',response=copy.deepcopy(row))]
        receipt.update(mode='ordinary-repair',responses=[row],tools=[],events=events,
                       quality=receipt['quality'][1:])
        return [receipt,copy.deepcopy(events),dict(calls=[],methods=[]),retained,before,
                dict(after,admitted=1,settled=14)]

    def test_authentic_history_cannot_replay_or_change(self):
        sample=self.sample();self.assertTrue(c.observe_repair(*sample)['retained_history_authenticated'])
        sample=self.sample();sample[2]['methods'].append('tools/call')
        with self.assertRaises(Exception):c.observe_repair(*sample)
        sample=self.sample();sample[3][0]['output']='invented'
        with self.assertRaises(Exception):c.observe_repair(*sample)

    def test_one_real_user_job_and_complete_cleanup(self):
        for key in ('admitted','settled','reserved','sessions','host_bytes'):
            sample=self.sample();sample[5][key]+=1
            with self.assertRaises(Exception):c.observe_repair(*sample)
        sample=self.sample();sample[0]['events'][3]['item']['content'][0]['text']='old task'
        sample[1]=copy.deepcopy(sample[0]['events'])
        with self.assertRaises(Exception):c.observe_repair(*sample)


if __name__=='__main__':unittest.main()
