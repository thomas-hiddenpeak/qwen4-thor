"""Counterexamples for the trace acceptance checker; all model rows are fixtures."""
import copy
import hashlib
import json
import unittest

from tools.serve_realtime import tracing_checks as c


def fixture(mode='primary'):
    session='sess_cpu';trace='trace_'+hashlib.sha256(session.encode()).hexdigest()
    source=dict(path='docs/MODEL.md',text='1: # Title\n2: \n3: Note',citation='[docs/MODEL.md:L1-L3]')
    record=dict(ok=True,source=source)
    call=dict(id='call_cpu',type='mcp_call',name='read_project_record',
              server_label='project',arguments=json.dumps(c.ARGUMENTS))
    actual=dict(call,output=json.dumps(dict(structuredContent=record)),approval_request_id='approval_cpu')
    approval=dict(id='approval_cpu',type='mcp_approval_request')
    usage=dict(input_tokens=12,output_tokens=2,total_tokens=14)
    response=lambda identifier,output:dict(id=identifier,status='completed',usage=dict(usage),output=[output])
    message=lambda text:dict(type='message',content=[dict(type='output_text',text=text)])
    ordinary=response('resp_math',message('42'))
    tool=response('resp_tool',call);answer=response('resp_answer',message('Title [docs/MODEL.md:L1-L3]'))
    responses=[ordinary,tool,answer] if mode=='primary' else [tool,answer,ordinary]
    events=[]
    for r in responses:
        events.append(dict(type='response.created',response=dict(id=r['id'])))
        if r is tool:
            events.append(dict(type='response.mcp_call_arguments.delta',response_id=r['id'],
                               item_id=call['id'],delta=call['arguments']))
        else:events.append(dict(type='response.output_text.delta',response_id=r['id'],
                                delta=r['output'][0]['content'][0]['text']))
        events.append(dict(type='response.done',response=r))
        if r is tool:
            events.extend([dict(type='conversation.item.done',item=approval),
                dict(type='conversation.item.done',item=dict(type='mcp_approval_response',
                    approval_request_id=approval['id'],approve=True)),
                dict(type='response.mcp_call.in_progress',item_id=call['id']),
                dict(type='response.output_item.done',item=actual)])
    def span(kind,identifier):
        return dict(id='span_'+identifier,kind=kind,entity_id=identifier,parent_span_id=None,
            parent_entity_id=None,status='completed',phase='',approval_request_id=None,
            partial=False,started_at_ms=1000,ended_at_ms=1002,duration_ms=2,
            dispatched_at_ms=None,usage={})
    traced=responses if mode=='primary' else responses[:2]
    spans=[dict(span('response',r['id']),usage=dict(usage)) for r in traced]
    spans += [span('mcp_import','import_cpu'),span('mcp_approval',approval['id']),
        dict(span('mcp_call',call['id']),parent_entity_id=tool['id'],parent_span_id='span_'+tool['id'],
             approval_request_id=approval['id'],dispatched_at_ms=1001)]
    snapshot=dict(object='realtime.trace',observation_boundary='native_protocol_owner',version=1,
        id=trace,session_id=session,conversation_id='conv_cpu',workflow_name='q4t-project-query',
        group_id=mode,metadata=dict(case=mode,opaque='caller-owned-private-metadata'),status='in_progress',
        close_reason=None,started_at_ms=1000,ended_at_ms=None,duration_ms=None,revision=4,
        events_seen=20,dropped_span_events=0,collector_errors=0,spans=spans,
        persistence=dict(state='durable_snapshot',object_revision=2,snapshot_revision=4,
                         owner_interrupted=False,capture_interrupted=False,expires_at=5000))
    final=copy.deepcopy(snapshot);final.update(status='closed',close_reason='session_ended',ended_at_ms=1010,duration_ms=10)
    ops=[]
    def op(label,body,status=200,scope='owner'):
        ops.append(dict(label=label,body=body,status=status,scope=scope))
    op('retrieve',copy.deepcopy(snapshot));op('summary',dict(data=[dict(id=trace,summary=True)]))
    op('cross-scope-get',{},404,'other');op('cross-scope-delete',{},404,'other')
    op('cross-scope-list',dict(data=[]),scope='other')
    quality=[dict(case='ordinary',answer='42',status='PASS'),
             dict(case='document',answer='Title [docs/MODEL.md:L1-L3]',status='PASS')]
    if mode=='primary':op('storage-unavailable',{},503)
    else:
        quality.reverse();op('worker-recovered-0',snapshot)
        op('delete-active',dict(deleted=True));op('no-resurrection',{},404);op('closed-still-deleted',{},404)
    receipt=dict(status='PASS',mode=mode,transport='official-openai-wss-and-http',connections=1,
        close_code=1000,automatic_replay=False,session_id=session,trace_id=trace,
        signals=['repair-storage' if mode=='primary' else 'kill-trace-worker'],responses=responses,
        tools=[actual],quality=quality,operations=ops,snapshot=snapshot,final_snapshot=final,
        retained=dict(status='closed'),events=events)
    before=dict(admitted=0,settled=0,reserved=0,active=0,sessions=0,host_bytes=0,free_slots=1)
    return [receipt,copy.deepcopy(events),dict(calls=[record],methods=['tools/call']),source,
            before,dict(before,admitted=3,settled=42)]


class TraceEvidence(unittest.TestCase):
    def reject(self,value):
        value[1]=copy.deepcopy(value[0]['events'])
        with self.assertRaises((RuntimeError,KeyError,ValueError,StopIteration)):c.observe(*value)

    def test_both_frozen_shapes(self):
        for mode in ('primary','light'):
            self.assertEqual(c.observe(*fixture(mode))['settled_tokens'],42)

    def test_trace_requires_confirmed_storage_and_revision(self):
        for key,value in [('state','queued'),('snapshot_revision',3),('capture_interrupted',True)]:
            sample=fixture();sample[0]['snapshot']['persistence'][key]=value;self.reject(sample)

    def test_payloads_are_not_trace_fields(self):
        for field in ('arguments','output','audio','prompt','authorization'):
            sample=fixture();sample[0]['snapshot']['spans'][-1][field]='private';self.reject(sample)
        sample=fixture();sample[0]['snapshot']['prompt']='private';self.reject(sample)

    def test_trace_usage_must_match_wire(self):
        for value in (1,-1,'14',True):
            sample=fixture();sample[0]['snapshot']['spans'][0]['usage']['total_tokens']=value;self.reject(sample)

    def test_missing_duplicate_or_partial_span_rejected(self):
        for mode in ('missing','duplicate','partial'):
            sample=fixture();spans=sample[0]['snapshot']['spans']
            if mode=='missing':spans.pop()
            elif mode=='duplicate':spans.append(copy.deepcopy(spans[0]))
            else:spans[0]['partial']=True
            self.reject(sample)

    def test_wrong_scope_and_approval_owner_rejected(self):
        sample=fixture();sample[0]['snapshot']['session_id']='foreign';self.reject(sample)
        for field in ('parent_entity_id','parent_span_id','approval_request_id'):
            sample=fixture();sample[0]['snapshot']['spans'][-1][field]='foreign';self.reject(sample)
        sample=fixture();sample[0]['operations'][2]['status']=200;self.reject(sample)

    def test_summary_cannot_hide_full_payload(self):
        sample=fixture();sample[0]['operations'][1]['body']['data'][0]['metadata']={};self.reject(sample)

    def test_deleted_capture_cannot_resurrect(self):
        for label in ('no-resurrection','closed-still-deleted'):
            sample=fixture('light');next(r for r in sample[0]['operations'] if r['label']==label)['status']=200
            self.reject(sample)

    def test_missing_fault_or_restart_is_not_recovery(self):
        sample=fixture();sample[0]['signals']=[];self.reject(sample)
        sample=fixture('light');sample[0]['retained']['status']='in_progress';self.reject(sample)
        sample=fixture('light');sample[0]['operations']=[r for r in sample[0]['operations']
            if not r['label'].startswith('worker-recovered-')];self.reject(sample)

    def test_replay_or_false_settlement_rejected(self):
        sample=fixture();sample[2]['calls']*=2;self.reject(sample)
        for key in ('admitted','settled','active','sessions','host_bytes','reserved','free_slots'):
            sample=fixture();sample[5][key]+=1;self.reject(sample)

    def test_stream_terminal_or_tool_order_mismatch_rejected(self):
        sample=fixture();sample[0]['events'][1]['delta']='wrong';self.reject(sample)
        sample=fixture();events=sample[0]['events'];event=next(e for e in events if
            e['type']=='response.output_item.done');events.remove(event);events.insert(0,event);self.reject(sample)

    def test_quality_failure_preserved_without_fabricating_protocol_failure(self):
        sample=fixture();receipt=sample[0]
        receipt['responses'][0]['output'][0]['content'][0]['text']='wrong'
        receipt['events'][1]['delta']='wrong';sample[1]=copy.deepcopy(receipt['events'])
        receipt['quality'][0].update(answer='wrong',status='FAIL');receipt['status']='QUALITY_FAIL'
        self.assertEqual(c.observe(*sample)['quality'][0]['status'],'FAIL')


if __name__=='__main__':unittest.main()
