"""Driver boundary checks without model, sockets or HTTP listeners."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_clients import managed_stage as s


class Driver(unittest.TestCase):
    def setUp(self):
        self.profiles=copy.deepcopy(s.soak.PROFILES)
        self.phase=s.soak.PHASE
        s.configure('short')
        root=s.BASE/'driver-fixtures';root.mkdir(exist_ok=True)
        self.root=Path(tempfile.mkdtemp(dir=root))

    def tearDown(self):
        s.soak.PROFILES=self.profiles
        s.soak.PHASE=self.phase
        s.soak.configure('short')

    def test_short_and_day_are_distinct_and_day_requires_explicit_operator(self):
        s.configure('short')
        self.assertEqual((s.LIMITS['starts'],s.LIMITS['generations'],s.soak.CYCLES),(1,12,2))
        self.assertEqual(s.PROFILES['short']['seconds'],30)
        short=s.OUT
        s.configure('day')
        self.assertNotEqual(s.OUT,short)
        self.assertEqual((s.LIMITS['generations'],s.soak.CYCLES,s.soak.SECONDS),(28802,5760,86400))
        with self.assertRaisesRegex(RuntimeError,'operator-run-day'):
            s.require_operator('day',False)
        s.require_operator('short',False);s.require_operator('day',True)

    def test_cycle_uses_two_clients_then_strict_and_tools(self):
        seen=[]
        class Fixture:
            resources=[];server=None;expected_start=0
            def preflight(self,label):seen.append(('preflight',label))
            def concurrent_requests(self):return ThreadPoolExecutor(max_workers=2)
            def complete(self,label,body,client=0):
                seen.append(('generation',label,copy.deepcopy(body),client))
                return SimpleNamespace(finish_reason='stop',message={'content':'{"ok":true}' if label.endswith('strict') else '710003'})
            def tools(self,label):seen.append(('tools',label))
            def drain(self,label,expected):
                self.expected=expected
                return {'q4t_structured_workers_started_total':3,'q4t_structured_workers_reaped_total':3}
            def control_get(self,label,path):pass
        fixture=Fixture()
        with mock.patch.object(s,'OUT',self.root),mock.patch.object(s.evidence.observe,'sample',return_value={}),\
             mock.patch.object(s.evidence,'values',return_value={'fixture':True}):
            s.Run.cycle(fixture,1)
        calls=[row for row in seen if row[0]=='generation']
        self.assertEqual(len(calls),3)
        self.assertEqual(sorted(row[3] for row in calls[:2]),[0,1])
        self.assertEqual(next(row[2]['max_tokens'] for row in calls if row[3]==1),64)
        self.assertEqual(calls[-1][1],'cycle-0001/strict')
        self.assertEqual(seen[-1],('tools','cycle-0001/project'))
        self.assertEqual(len({row[2]['request_id'] for row in calls}),3)
        self.assertEqual(fixture.expected,5)

    def preflight(self,limit):
        seen={}
        body=json.loads((s.ROOT/'.q4t-work/serve-clients-20261104/short/beta-default-wire.bin').read_text())
        body['output']['requested_tokens']=limit
        class Connection:
            def request(self,method,path,content,headers):seen.update(method=method,path=path,body=json.loads(content),headers=headers)
            def getresponse(self):return SimpleNamespace(status=200,read=lambda _:json.dumps(body).encode())
            def close(self):seen['closed']=True
        fixture=SimpleNamespace(keys={'server_name':'fixture','clients':[{'api_key':'alpha'},{'api_key':'beta'}]},
                                context=None,budget=lambda kind:None)
        with mock.patch.object(s,'OUT',self.root),mock.patch.object(s.gw,'TLSConnection',return_value=Connection()):
            s.Run.preflight(fixture,'beta')
        return seen

    def test_preflight_uses_beta_key_and_omits_max_tokens(self):
        value=self.preflight(64)
        self.assertEqual(value['headers']['Authorization'],'Bearer beta')
        self.assertEqual(value['path'],'/v1/chat/preflight')
        self.assertNotIn('max_tokens',value['body']['request'])
        self.assertTrue(value['closed'])

    def test_wrong_effective_budget_is_not_a_success(self):
        with self.assertRaisesRegex(RuntimeError,'wrong client budget'):
            self.preflight(256)

    def test_all_frontends_stop_before_backend_releases_lease(self):
        seen=[]
        fixture=SimpleNamespace(
            rejected_fronts=lambda:seen.append('negative'),
            start_front=lambda n:seen.append(('start',n)),
            preflight=lambda *a,**k:None,soak=lambda:None,
            graceful=lambda:seen.append(('stop',1)),
            drain=lambda *a:{},complete=lambda *a:SimpleNamespace(message={'content':'710003'}),
            front_cli=lambda n,*a:seen.append(('stop',n)),
            wait_front=lambda *a,**k:{'cleanup_confirmed':True,'nginx_returncode':0},
            wait_gone=lambda n:seen.append(('gone',n)),
            stop_backend=lambda:seen.append('backend-stop'))
        with mock.patch.object(s,'OUT',self.root):s.Run.business(fixture)
        self.assertEqual(seen[-3:],[('stop',2),('gone',2),'backend-stop'])


if __name__=='__main__':
    unittest.main()
