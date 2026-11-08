import json
import os
import subprocess
import unittest
from tools.serve_custom_tools.acceptance import LIMITS, requests, require_fragments


class Driver(unittest.TestCase):
    def test_budget_and_actual_custom_formats(self):
        rows=requests()
        self.assertEqual(len(rows)+1,8)
        self.assertEqual(LIMITS,dict(starts=2,chats=8,controls=2000,seconds=900))
        for syntax in ('text','regex','lark'):
            tool=rows[syntax]['tools'][0]
            self.assertEqual(tool['type'],'custom')
            fmt=tool['custom']['format']
            self.assertEqual(fmt['type'],'text' if syntax=='text' else 'grammar')
            if syntax!='text':self.assertEqual(fmt['grammar']['syntax'],syntax)
        self.assertEqual(rows['truncated']['max_completion_tokens'],64)
        self.assertEqual(len(rows['mixed']['tools']),2)

    def test_repair_reuses_unchanged_cases_with_exact_cumulative_bound(self):
        from tools.serve_custom_tools.repair import REPAIR_LIMITS,CUMULATIVE_LIMITS
        self.assertEqual(REPAIR_LIMITS['starts']+1,CUMULATIVE_LIMITS['starts'])
        self.assertEqual(REPAIR_LIMITS['chats']+5,CUMULATIVE_LIMITS['chats'])
        self.assertLessEqual(REPAIR_LIMITS['seconds']+61,CUMULATIVE_LIMITS['seconds'])
        prompt=requests()['mixed']['messages'][0]
        self.assertEqual(prompt['role'],'developer')
        self.assertIn('<custom=read_project_record_regex>',prompt['content'])
        self.assertIn('<function=read_project_record>',prompt['content'])

    def test_frozen_requests_pass_production_preparation(self):
        lines=[json.dumps(dict(raw=json.dumps(row))) for row in requests().values()]
        run=subprocess.run([os.environ['Q4T_CUSTOM_TOOLS_PROBE']],input='\n'.join(lines)+'\n',
            capture_output=True,text=True,check=True,timeout=10)
        rows=[json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual(len(rows),7)
        self.assertTrue(all(row['accepted'] for row in rows),rows)

    def test_fragment_checks_reject_identity_and_terminal_corruption(self):
        def wire(repeat=False,kind='custom',finished=True):
            rows=[]
            for i,value in enumerate(('', 'READ ', 'docs/MODEL.md 1 16')):
                part=dict(index=0,custom=dict(input=value))
                if i==0 or repeat:part.update(id='c',type=kind);part['custom']['name']='read'
                rows.append(dict(choices=[dict(delta=dict(tool_calls=[part]),finish_reason=None)]))
            if finished:rows.append(dict(choices=[dict(delta={},finish_reason='tool_calls')]))
            return ''.join('data: '+json.dumps(row)+'\r\n\r\n' for row in rows).encode()
        self.assertEqual(require_fragments(wire())[0]['parts'],['','READ ','docs/MODEL.md 1 16'])
        for value in (wire(repeat=True),wire(finished=False)):
            with self.assertRaises(RuntimeError):require_fragments(value)


if __name__=='__main__':unittest.main()
