import json
import os
import subprocess
import unittest

from tools.serve_legacy_tools.acceptance import LIMITS, requests, require_fragments


class Driver(unittest.TestCase):
    def test_frozen_requests_and_budget(self):
        rows=requests()
        self.assertEqual(len(rows)+2,7)  # Result-derived followup and migration.
        self.assertLessEqual(7,LIMITS['chats'])
        self.assertEqual(rows['tool']['function_call'],dict(name='read_project_record'))
        self.assertEqual(rows['automatic']['function_call'],'auto')
        self.assertEqual(rows['truncated']['max_completion_tokens'],64)
        self.assertNotIn('strict',rows['tool']['functions'][0])

    def test_requests_reach_production_parser(self):
        lines=[json.dumps(dict(raw=json.dumps(row))) for row in requests().values()]
        run=subprocess.run([os.environ['Q4T_LEGACY_TOOLS_PROBE']],input='\n'.join(lines)+'\n',
            capture_output=True,text=True,check=True,timeout=10)
        rows=[json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual(len(rows),5)
        self.assertTrue(all(row['accepted'] for row in rows),rows)

    def test_incremental_assertions_reject_wrong_shape_and_identity(self):
        def wire(parts, repeat=False, modern=False):
            events=[]
            for i,part in enumerate(parts):
                function=dict(arguments=part)
                if i==0 or repeat:function['name']='read'
                delta=dict(function_call=function)
                if modern:delta=dict(tool_calls=[dict(index=0,id='a',type='function',function=function)])
                events.append(dict(choices=[dict(delta=delta,finish_reason=None)]))
            events.append(dict(choices=[dict(delta={},finish_reason='function_call')]))
            return ''.join('data: '+json.dumps(e)+'\r\n\r\n' for e in events).encode()
        self.assertEqual(len(require_fragments(wire(['{','"a":1','}']))),3)
        for raw in (wire(['{"a":1}']),wire(['{','"a":1','}'],True),
                    wire(['{','"a":1','}'],modern=True)):
            with self.assertRaises(RuntimeError):require_fragments(raw)


if __name__=='__main__':unittest.main()
