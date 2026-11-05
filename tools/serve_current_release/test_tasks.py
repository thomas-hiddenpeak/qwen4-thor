"""Explicit current protocol and exact 17/18-source reader migrations."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from tools.project_tasks import test_protocol as old
from tools.project_tasks import engine,store,migration,protocol,migration_fixtures as history
from tools.serve_service import package as pkg

REVIEW=json.loads((store.ROOT/protocol.REGISTRY).read_bytes())['releases'][3]


class Versions:
    def setUp(self):
        for name,value in (('REVIEW',REVIEW),('BEFORE',REVIEW['from_protocol_sha256']),
                           ('AFTER',REVIEW['to_protocol_sha256'])):
            change=patch.object(old,name,value);change.start();self.addCleanup(change.stop)
        super().setUp()


class Current(Versions,old.Protocol):
    def test_registered_17_source_reader_migrates_protocol_and_source_together(self):
        selected=history.release(10)
        runtime=self.directory/'reader-17';extracted=history.extract_runtime(10,runtime)
        self.assertEqual(len(extracted['sources']),17)
        self.assertEqual({n:pkg.sha(runtime/n) for n in extracted['sources']},selected['runtime_sources'])
        self.root=self.directory/'old-reader-task';store.init(self.root)
        spec=dict(self.spec,runtime_sources=selected['runtime_sources'])
        store.create(self.root,self.name,spec)
        with patch.object(engine,'sources',return_value=selected['runtime_sources']):
            self.break_at('tool_result')
        self.plan();moved=self.apply()
        self.assertEqual((moved['requests'],moved['saved_tool_results']),(1,1))
        self.binding=self.target
        done=self.execute()
        self.assertEqual((done['requests'],self.count(),done['migrations']),(2,1,1))

    def test_pre_registry_18_source_reader_requires_explicit_source_migration(self):
        selected=history.release(11)
        runtime=self.directory/'reader-18';extracted=history.extract_runtime(11,runtime)
        self.assertEqual(len(extracted['sources']),18)
        self.binding=dict(self.binding,protocol_sha256=REVIEW['to_protocol_sha256'])
        self.root=self.directory/'client-reader-task';store.init(self.root)
        spec=engine.specification(self.binding,'read current',stream=False)
        spec['runtime_sources']=selected['runtime_sources'];store.create(self.root,self.name,spec)
        with patch.object(engine,'sources',return_value=selected['runtime_sources']):
            self.break_at('tool_result')
        client=old.Client(self.binding)
        with self.assertRaises(store.ContractError) as caught:self.execute(client)
        self.assertEqual(caught.exception.code,'runtime_changed')
        self.assertEqual(client.calls,[])
        path=self.directory/'source-only.json'
        prepared=migration.prepare(self.root,self.binding,'key',path,'reviewed source closure')
        moved=migration.apply(self.root,self.binding,'key',path,prepared['plan_sha256'])
        self.assertTrue(moved['no_execution'])
        done=self.execute(client)
        self.assertEqual((done['requests'],self.count(),len(client.calls)),(2,1,1))

    def test_current_review_has_no_implicit_transitive_or_reverse_upgrade(self):
        steps=json.loads((store.ROOT/protocol.REGISTRY).read_bytes())['releases']
        origin=dict(self.binding,protocol_sha256=steps[0]['from_protocol_sha256'])
        target=dict(origin,protocol_sha256=REVIEW['to_protocol_sha256'])
        for before,after in ((origin,target),(target,self.binding)):
            with self.assertRaises(store.ContractError) as caught:protocol.reviewed(before,after)
            self.assertEqual(caught.exception.code,'protocol_unsupported')
        self.assertEqual(pkg.sha(store.ROOT/REVIEW['evidence']),REVIEW['archive_sha256'])


class Archived(Versions,old.Archived):pass
class Commands(Versions,old.Commands):pass


def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite()
    for name in loader.getTestCaseNames(Current):
        if name!='test_seven_historical_source_snapshots_and_actual_11g_final_ledger':
            suite.addTest(Current(name))
    for cls,original in ((Archived,old.Archived),(Commands,old.Commands)):
        for name in original.__dict__:
            if name.startswith('test_'):suite.addTest(cls(name))
    return suite
