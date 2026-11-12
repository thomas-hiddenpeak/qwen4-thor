"""Legacy functions retain the same complete-response execution barrier."""
import copy
import io
import json
import unittest
from unittest.mock import patch

from tools.examples.chat_client import (ProtocolError, RemoteError, read_json,
    read_sse, prepare_tool_calls, append_tool_results, append_final)

FUNCTION = dict(name="read", arguments='{"path":"中国/MODEL.md"}')
COUNTS = dict(prompt_tokens=5, completion_tokens=4, total_tokens=9)


def body():
    return dict(id="legacy", object="chat.completion", model="m", created=1,
        choices=[dict(index=0,finish_reason="function_call", logprobs=None,
            message=dict(role="assistant",content=None,function_call=FUNCTION))],usage=COUNTS)


def wire(*, done=True, finish="function_call", error=False, mixed=False):
    rows=[]
    deltas=[dict(role="assistant", content=""),
        dict(function_call=dict(name="re", arguments="{")),
        dict(function_call=dict(name="ad",arguments='"path":"中国/')),
        dict(function_call=dict(arguments='MODEL.md"}'))]
    if mixed:deltas.append(dict(tool_calls=[dict(index=0,id="a",type="function",function=FUNCTION)]))
    if finish is not None:deltas.append({})
    for index,delta in enumerate(deltas):
        rows.append(dict(id="legacy",object="chat.completion.chunk",model="m",created=1,
            choices=[dict(index=0,delta=delta,logprobs=None,
                finish_reason=finish if index==len(deltas)-1 and finish is not None else None)],usage=None))
    if error:rows.append(dict(error=dict(message="cancelled",type="server_error",code="cancelled",param=None)))
    elif finish is not None:rows.append(dict(rows[0],choices=[],usage=COUNTS))
    return ("".join("data: "+json.dumps(row,ensure_ascii=False)+"\r\n\r\n" for row in rows)+
        ("data: [DONE]\r\n\r\n" if done else "")).encode()


class LegacyClients(unittest.TestCase):
    def test_bridge_compares_actual_legacy_tool_plan(self):
        from tools.serve_protocol.js_client import JsSdkClient, ROOT
        agent=JsSdkClient("http://127.0.0.1:1",ROOT/".q4t-work/serve-protocol-20261103/sdk")
        function=dict(name="read_project_record",arguments=json.dumps(dict(
            path="docs/MODEL.md",start_line=1,line_count=8)))
        result=dict(id="legacy",model="m",message=dict(role="assistant",content=None,
            function_call=function),finish_reason="function_call",usage=COUNTS)
        plan=dict(call_id="read_project_record",name="read_project_record",
            arguments=json.loads(function["arguments"]))
        with patch.object(agent,"call",return_value=dict(result=result,planned_tools=[plan])):
            self.assertEqual(agent.complete(dict(model="m")).finish_reason,"function_call")
        with patch.object(agent,"call",return_value=dict(result=result,planned_tools=[])):
            # js_client imports the standalone CLI module; its exception class
            # is a separate module instance from the package import above.
            with self.assertRaises(ValueError):agent.complete(dict(model="m"))

    def test_json_and_incremental_legacy_match_and_roundtrip(self):
        full=read_json(io.BytesIO(json.dumps(body()).encode()))
        streamed=read_sse(io.BytesIO(wire()))
        self.assertEqual(full,streamed)
        planned=prepare_tool_calls(streamed,{"read":lambda value:None})
        self.assertEqual(planned[0].call_id,"read")
        history=append_tool_results([],streamed,{"read":"真实记录"})
        self.assertEqual(history[1],dict(role="function",name="read",content="真实记录"))
        self.assertNotIn("tool_call_id",history[1])
        with self.assertRaises(ProtocolError):append_final([],streamed)

    def test_incomplete_cancelled_mixed_stream_never_returns_tool_plan(self):
        for raw in (wire(done=False),wire(finish=None),wire(error=True),wire(mixed=True)):
            with self.assertRaises((ProtocolError,RemoteError)):read_sse(io.BytesIO(raw))

    def test_malformed_and_wrong_terminal_reject(self):
        for mutate in (lambda r:r["choices"][0].update(finish_reason="tool_calls"),
            lambda r:r["choices"][0].update(finish_reason="stop"),
            lambda r:r["choices"][0]["message"]["function_call"].update(arguments="[]"),
            lambda r:r["choices"][0]["message"].update(tool_calls=[dict(id="a",type="function",function=FUNCTION)])):
            value=copy.deepcopy(body());mutate(value)
            with self.assertRaises(ProtocolError):read_json(io.BytesIO(json.dumps(value).encode()))

    def test_result_names_must_match_before_history_publication(self):
        result=read_sse(io.BytesIO(wire()))
        for replies in ({},{"wrong":"record"},{"read":"record","extra":"oops"}):
            with self.assertRaises(ProtocolError):append_tool_results([],result,replies)
        with self.assertRaises(ProtocolError):prepare_tool_calls(result,{})


if __name__=="__main__":unittest.main()
