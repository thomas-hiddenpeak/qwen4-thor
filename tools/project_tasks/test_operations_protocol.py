"""Directed operations-version upgrade, including prior protocol history."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tools.project_tasks import test_protocol as old
from tools.project_tasks import store, engine, migration, protocol
from tools.project_tasks import migration_fixtures as history
from tools.serve_service import package as pkg

REVIEW = json.loads((store.ROOT/protocol.REGISTRY).read_bytes())['releases'][1]


class Versions:
    def setUp(self):
        for field, value in (('REVIEW', REVIEW),
                ('BEFORE', REVIEW['from_protocol_sha256']),
                ('AFTER', REVIEW['to_protocol_sha256'])):
            change = patch.object(old, field, value)
            change.start()
            self.addCleanup(change.stop)
        super().setUp()


class Operations(Versions, old.Protocol):
    def test_explicit_two_hops_preserve_original_wire_and_budget(self):
        first = json.loads((store.ROOT/protocol.REGISTRY).read_bytes())['releases'][0]
        self.binding = dict(self.binding, protocol_sha256=first['from_protocol_sha256'])
        self.root = self.directory/'chain'
        store.init(self.root)
        store.create(self.root, self.name, engine.specification(
            self.binding, 'read current', stream=False))
        self.break_at('tool_result')
        with store.open_task(self.root, self.name) as ledger:
            original = ledger.wire(1).read_bytes()
            events = deepcopy(ledger.events())
        target = dict(self.binding, protocol_sha256=REVIEW['to_protocol_sha256'])
        with self.assertRaises(store.ContractError) as caught:
            protocol.reviewed(self.binding, target)
        self.assertEqual(caught.exception.code, 'protocol_unsupported')
        for index, review in enumerate((first, REVIEW)):
            self.plan(dict(self.binding, protocol_sha256=review['to_protocol_sha256']),
                      'hop-'+str(index))
            moved = self.apply()
            self.assertEqual((moved['migrations'], moved['requests'],
                              moved['saved_tool_results']), (index+1, 1, 1))
            self.binding = self.target
        client = old.Client(self.binding)
        done = self.execute(client)
        self.assertEqual((done['requests'], done['migrations'], self.count()), (2, 2, 1))
        self.assertEqual(len(client.calls), 1)
        with store.open_task(self.root, self.name) as ledger:
            self.assertEqual(ledger.wire(1).read_bytes(), original)
            self.assertEqual(ledger.events()[:len(events)], events)

    def test_actual_11h_history_and_exact_17_file_reader_remain_supported(self):
        selected = history.release(7)
        runtime = self.directory/'historical-reader'
        extracted = history.extract_runtime(7, runtime)
        self.assertEqual(len(extracted['sources']), 17)
        self.assertEqual({n: pkg.sha(runtime/n) for n in extracted['sources']},
                         selected['runtime_sources'])
        data = history.archive(7)
        exported = json.loads(data['execution/alpha-ledger.json'])
        directory = self.directory/'old-wire'
        directory.mkdir(mode=0o700)
        for name, raw in data.items():
            if name.startswith('execution/alpha-completed-task/') and \
                    Path(name).name.startswith(('wire-', 'check-')):
                store.write_new(directory/Path(name).name, raw)
        class Ledger:
            meta = exported['meta']
            spec = meta['spec']
            initial = store.digest(meta)
            path = directory
            def wire(self, number): return self.path/f'wire-{number:03d}.bin'
            def events(self): return exported['events']
        previous = Ledger.initial
        for row in Ledger().events():
            self.assertEqual(row['digest'], store.digest(
                [row['seq'], row['kind'], row['payload'], previous]))
            previous = row['digest']
        state = engine.replay(Ledger())
        self.assertEqual((state['phase'], state['requests'], state['tool_results'],
                          state['migrations']), ('COMPLETED', 2, 1, 1))
        spec = state['execution_spec']
        self.assertEqual(spec['runtime_sources'], selected['runtime_sources'])
        self.assertEqual(migration.supported(spec, engine.sources()), selected['release'])
        self.assertEqual(spec['binding']['protocol_sha256'], REVIEW['from_protocol_sha256'])
        self.assertEqual(protocol.reviewed(spec['binding'], dict(spec['binding'],
            protocol_sha256=REVIEW['to_protocol_sha256'])), REVIEW)
        self.assertEqual(pkg.sha(store.ROOT/REVIEW['evidence']), REVIEW['archive_sha256'])


class Archived(Versions, old.Archived):
    pass


class Commands(Versions, old.Commands):
    pass


def load_tests(loader, tests, pattern):
    # Reuse behavior checks with the new exact protocol pair; do not count
    # inherited tests twice or repeat unaffected pre-11h historical fixtures.
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(Operations):
        if name != 'test_seven_historical_source_snapshots_and_actual_11g_final_ledger':
            suite.addTest(Operations(name))
    for cls, original in ((Archived, old.Archived), (Commands, old.Commands)):
        for name in original.__dict__:
            if name.startswith('test_'):
                suite.addTest(cls(name))
    return suite
