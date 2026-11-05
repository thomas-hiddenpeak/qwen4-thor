"""Directed startup-listener protocol migration preserves durable task history."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tools.project_tasks import test_protocol as old
from tools.project_tasks import engine,store,migration,protocol,migration_fixtures as history
from tools.serve_service import package as pkg

REVIEW=json.loads((store.ROOT/protocol.REGISTRY).read_bytes())['releases'][2]


class Versions:
    def setUp(self):
        for name,value in (('REVIEW',REVIEW),('BEFORE',REVIEW['from_protocol_sha256']),
                           ('AFTER',REVIEW['to_protocol_sha256'])):
            change=patch.object(old,name,value);change.start();self.addCleanup(change.stop)
        super().setUp()


class Startup(Versions,old.Protocol):
    def test_three_explicit_hops_preserve_wire_budget_and_one_tool(self):
        steps=json.loads((store.ROOT/protocol.REGISTRY).read_bytes())['releases'][:3]
        self.binding=dict(self.binding,protocol_sha256=steps[0]['from_protocol_sha256'])
        self.root=self.directory/'chain';store.init(self.root)
        store.create(self.root,self.name,engine.specification(self.binding,'read current',stream=False))
        self.break_at('tool_result')
        with store.open_task(self.root,self.name) as ledger:
            raw=ledger.wire(1).read_bytes();events=deepcopy(ledger.events())
        target=dict(self.binding,protocol_sha256=REVIEW['to_protocol_sha256'])
        with self.assertRaises(store.ContractError) as caught:protocol.reviewed(self.binding,target)
        self.assertEqual(caught.exception.code,'protocol_unsupported')
        for index,step in enumerate(steps):
            self.plan(dict(self.binding,protocol_sha256=step['to_protocol_sha256']),'hop-'+str(index))
            moved=self.apply();self.binding=self.target
            self.assertEqual((moved['migrations'],moved['requests'],moved['saved_tool_results']),
                             (index+1,1,1))
        client=old.Client(self.binding);done=self.execute(client)
        self.assertEqual((done['requests'],done['migrations'],self.count(),len(client.calls)),(2,3,1,1))
        with store.open_task(self.root,self.name) as ledger:
            self.assertEqual(ledger.wire(1).read_bytes(),raw)
            self.assertEqual(ledger.events()[:len(events)],events)

    def test_actual_11j_reader_and_final_ledger_match_registered_sources(self):
        selected=history.release(9);runtime=self.directory/'historical-reader'
        extracted=history.extract_runtime(9,runtime)
        self.assertEqual(len(extracted['sources']),17)
        self.assertEqual({n:pkg.sha(runtime/n) for n in extracted['sources']},selected['runtime_sources'])
        data=history.archive(9);exported=json.loads(data['execution/beta-ledger.json'])
        directory=self.directory/'old-wire';directory.mkdir(mode=0o700)
        for name,raw in data.items():
            if name.startswith('execution/beta-completed-task/') and Path(name).name.startswith(('wire-','check-')):
                store.write_new(directory/Path(name).name,raw)
        class Ledger:
            meta=exported['meta'];spec=meta['spec'];initial=store.digest(meta);path=directory
            def wire(self,number):return self.path/f'wire-{number:03d}.bin'
            def events(self):return exported['events']
        previous=Ledger.initial
        for row in Ledger().events():
            self.assertEqual(row['digest'],store.digest([row['seq'],row['kind'],row['payload'],previous]))
            previous=row['digest']
        state=engine.replay(Ledger());spec=state['execution_spec']
        self.assertEqual((state['phase'],state['requests'],state['tool_results'],state['migrations']),
                         ('COMPLETED',2,1,0))
        self.assertEqual(spec['runtime_sources'],selected['runtime_sources'])
        self.assertEqual(migration.supported(spec,engine.sources()),selected['release'])
        self.assertEqual(spec['binding']['protocol_sha256'],REVIEW['from_protocol_sha256'])
        self.assertEqual(protocol.reviewed(spec['binding'],dict(spec['binding'],
            protocol_sha256=REVIEW['to_protocol_sha256'])),REVIEW)
        self.assertEqual(pkg.sha(store.ROOT/REVIEW['evidence']),REVIEW['archive_sha256'])


class Archived(Versions,old.Archived):pass
class Commands(Versions,old.Commands):pass


def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite()
    for name in loader.getTestCaseNames(Startup):
        if name!='test_seven_historical_source_snapshots_and_actual_11g_final_ledger':
            suite.addTest(Startup(name))
    for cls,original in ((Archived,old.Archived),(Commands,old.Commands)):
        for name in original.__dict__:
            if name.startswith('test_'):suite.addTest(cls(name))
    return suite
