import json
import os
import subprocess
import unittest

from tools.serve_tool_stream.acceptance import LIMITS, requests, require_fragments


class Driver(unittest.TestCase):
    def test_frozen_requests_and_budget(self):
        rows=requests()
        self.assertEqual(len(rows)+1,7)  # Actual result-derived followup.
        self.assertLessEqual(7,LIMITS['chats'])
        self.assertEqual(rows['tool']['tool_choice']['allowed_tools']['mode'],'required')
        self.assertEqual(rows['automatic']['tool_choice']['allowed_tools']['mode'],'auto')
        self.assertTrue(rows['parallel']['parallel_tool_calls'])
        self.assertEqual(rows['truncated']['max_completion_tokens'],64)

    def test_request_shapes_reach_production_parser(self):
        lines=[json.dumps(dict(raw=json.dumps(row))) for row in requests().values()]
        run=subprocess.run([os.environ['Q4T_TOOL_STREAM_PROBE']],input='\n'.join(lines)+'\n',
            capture_output=True,text=True,check=True,timeout=10)
        rows=[json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual(len(rows),6)
        self.assertTrue(all(row['accepted'] for row in rows),rows)

    def test_incremental_assertion_rejects_whole_call_and_identity_replay(self):
        def wire(parts, repeat=False):
            events=[]
            for i,part in enumerate(parts):
                call=dict(index=0,function=dict(arguments=part))
                if i==0 or repeat:call.update(id='a',type='function');call['function']['name']='read'
                events.append(dict(choices=[dict(delta=dict(tool_calls=[call]),finish_reason=None)]))
            events.append(dict(choices=[dict(delta={},finish_reason='tool_calls')]))
            return ''.join('data: '+json.dumps(e)+'\r\n\r\n' for e in events).encode()
        self.assertEqual(len(require_fragments(wire(['{','"a":1','}']),1)[0]),3)
        for raw in (wire(['{"a":1}']),wire(['{','"a":1','}'],True)):
            with self.assertRaises(RuntimeError):require_fragments(raw,1)


if __name__=='__main__':unittest.main()
