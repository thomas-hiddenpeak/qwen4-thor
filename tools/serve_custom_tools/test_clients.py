"""Custom raw text is validated explicitly; terminal/EOF still gate execution."""
import copy
import io
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/examples'))
from chat_client import (ProtocolError, RemoteError, read_json, read_sse,
                        prepare_tool_calls, append_tool_results)
from project_records import (ProjectRecords, CUSTOM_VALIDATORS, parse_custom_read)

COUNTS = dict(prompt_tokens=5, completion_tokens=8, total_tokens=13)
INPUT = 'READ "中国"\n</tool_call>\\😀\x00'
CALLS = [dict(id='custom0', type='custom', custom=dict(name='read', input=INPUT)),
         dict(id='function1', type='function', function=dict(name='noop', arguments='{}'))]


def body(calls=None):
    return dict(id='custom', object='chat.completion', created=1, model='m',
        choices=[dict(index=0, finish_reason='tool_calls', logprobs=None,
            message=dict(role='assistant', content=None, tool_calls=copy.deepcopy(CALLS if calls is None else calls)))],
        usage=COUNTS)


def wire(*, done=True, finish='tool_calls', extra=None):
    def chunk(delta, terminal=None):
        return dict(id='custom',object='chat.completion.chunk',created=1,model='m',usage=None,
            choices=[dict(index=0,delta=delta,finish_reason=terminal,logprobs=None)])
    rows=[chunk(dict(role='assistant',content='')),
        chunk(dict(tool_calls=[dict(index=0,id='custom0',type='custom',custom=dict(name='read',input=''))]))]
    for c in INPUT: rows.append(chunk(dict(tool_calls=[dict(index=0,custom=dict(input=c))])))
    rows.append(chunk(dict(tool_calls=[dict(index=1,**CALLS[1])])))
    if finish:rows.append(chunk({},finish))
    if extra:rows.append(extra)
    if finish:rows.append(dict(rows[0],choices=[],usage=COUNTS))
    return (''.join('data: '+json.dumps(r,ensure_ascii=False)+'\r\n\r\n' for r in rows)+
        ('data: [DONE]\r\n\r\n' if done else '')).encode()


class CustomClients(unittest.TestCase):
    def test_bridge_compares_custom_plan_and_preserves_input_type(self):
        from tools.serve_protocol.js_client import JsSdkClient
        agent=JsSdkClient("http://127.0.0.1:1",ROOT/".q4t-work/serve-protocol-20261103/sdk")
        call=dict(id="custom",type="custom",custom=dict(name="read_project_record_text",input="READ docs/MODEL.md 1 8"))
        value=dict(id="custom",model="m",message=dict(role="assistant",content=None,tool_calls=[call]),finish_reason="tool_calls",usage=COUNTS)
        plan=dict(call_id="custom",type="custom",name=call["custom"]["name"],input=call["custom"]["input"])
        with patch.object(agent,"call",return_value=dict(result=value,planned_tools=[plan])):
            self.assertEqual(agent.complete(dict(model="m")).finish_reason,"tool_calls")
        with patch.object(agent,"call",return_value=dict(result=value,planned_tools=[dict(call_id="custom",name=plan["name"],arguments=plan["input"])])):
            with self.assertRaises(ValueError):agent.complete(dict(model="m"))

    def test_raw_custom_and_mixed_function_roundtrip(self):
        full=read_json(io.BytesIO(json.dumps(body()).encode()))
        streamed=read_sse(io.BytesIO(wire()))
        self.assertEqual(full,streamed)
        plans=prepare_tool_calls(streamed,{'noop':lambda value:None},custom_validators={'read':lambda value:None})
        self.assertEqual(plans[0].input,INPUT)
        self.assertFalse(hasattr(plans[0],'arguments'))
        self.assertEqual(plans[1].arguments,{})
        history=append_tool_results([],streamed,{'custom0':'实际资料','function1':'done'})
        self.assertEqual(history[0]['tool_calls'],CALLS)
        self.assertEqual([x['tool_call_id'] for x in history[1:]],['custom0','function1'])

    def test_custom_requires_separate_explicit_registry(self):
        value=read_sse(io.BytesIO(wire()))
        for validators in ({},{'read':lambda value:False}):
            with self.assertRaises(ProtocolError):
                prepare_tool_calls(value,{'read':lambda value:None,'noop':lambda value:None},custom_validators=validators)

    def test_incomplete_error_or_length_never_authorizes_execution(self):
        for raw in (wire(done=False),wire(finish=None),wire(finish='length'),
            wire(extra=dict(error=dict(message='cancel',code='cancelled',type='server_error',param=None)))):
            with self.assertRaises((ProtocolError,RemoteError)):
                result=read_sse(io.BytesIO(raw))
                prepare_tool_calls(result,{'noop':lambda value:None},custom_validators={'read':lambda value:None})

    def test_malformed_types_and_fields_rejected(self):
        for change in (lambda c:c['custom'].update(input={}),lambda c:c.update(type='function'),
                       lambda c:c.update(function=dict(name='read',arguments='{}')),
                       lambda c:c['custom'].update(arguments='{}')):
            value=body();change(value['choices'][0]['message']['tool_calls'][0])
            with self.assertRaises(ProtocolError):read_json(io.BytesIO(json.dumps(value).encode()))

    def test_actual_read_only_custom_executor_and_limits(self):
        call=dict(id='real',type='custom',custom=dict(name='read_project_record_lark',input='READ docs/MODEL.md 1 8'))
        value=read_json(io.BytesIO(json.dumps(body([call])).encode()))
        plan=prepare_tool_calls(value,{},custom_validators=CUSTOM_VALIDATORS)[0]
        result=ProjectRecords(ROOT).execute(plan)
        self.assertTrue(result['ok'])
        self.assertIn('Qwen',json.dumps(result,ensure_ascii=False))
        for invalid in ('READ docs/MODEL.md 0 8','READ docs/MODEL.md 1 81',
            'READ /etc/passwd 1 8','READ docs/MODEL.md 1 8\n','READ docs/MODEL.md 1 8; rm'):
            with self.assertRaises(ProtocolError):parse_custom_read(invalid)
        # Path policy still resides in the actual filesystem executor.
        call['custom']['input']='READ docs/../docs/MODEL.md 1 8'
        plan=prepare_tool_calls(read_json(io.BytesIO(json.dumps(body([call])).encode())),{},custom_validators=CUSTOM_VALIDATORS)[0]
        self.assertFalse(ProjectRecords(ROOT).execute(plan)['ok'])


if __name__=='__main__':unittest.main()
