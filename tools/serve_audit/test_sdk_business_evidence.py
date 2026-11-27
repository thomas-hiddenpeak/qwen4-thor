"""Prevent cross-method or cross-version promotion of incomplete wire evidence."""
import csv
import io
import unittest

from .sdk_business_evidence import Catalog, CLIENTS, index, render


class BusinessEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog([('GET', '/agents/{id}'), ('GET', '/agents/automations'),
                                ('POST', '/responses'), ('POST', '/responses?beta=true')])

    def wire(self, method='POST', path='/v1/responses', **changes):
        return dict(method=method, path=path, trace='req-1', status=200, **changes)

    def test_public_query_selectors(self):
        match = self.catalog.match
        self.assertIsNone(match(self.wire()))
        self.assertEqual(match(self.wire(query='')), ('POST', '/responses'))
        self.assertEqual(match(self.wire(query='limit=1&beta=true')), ('POST', '/responses?beta=true'))
        for query in ('beta=false', 'beta=true&beta=true', 'beta='):
            with self.subTest(query=query), self.assertRaises(ValueError):
                match(self.wire(query=query))

    def test_static_path_beats_id_and_method_must_exist(self):
        self.assertEqual(self.catalog.match(self.wire('GET', '/v1/agents/automations')),
                         ('GET', '/agents/automations'))
        self.assertEqual(self.catalog.match(self.wire('GET', '/v1/agents/agent_123')),
                         ('GET', '/agents/{id}'))
        with self.assertRaises(ValueError):
            self.catalog.match(self.wire('POST', '/v1/agents/automations'))

    def test_ambiguous_parameter_shapes_rejected(self):
        for patterns in [('/things/{one}', '/things/{two}'), ('/{name}/1', '/things/{id}')]:
            with self.subTest(patterns=patterns), self.assertRaisesRegex(ValueError, 'ambiguous'):
                Catalog([('GET', p) for p in patterns]).match(self.wire('GET', '/v1/things/1'))

    def test_missing_queries_are_retained_unassigned(self):
        item = self.wire()
        counts, missing = index(self.catalog, [('uploads', (588, [(CLIENTS[0], item)], {'200': 1}))])
        self.assertFalse(counts)
        self.assertEqual((missing[0]['trace'], missing[0]['sdk'], missing[0]['revision']),
                         ('req-1', CLIENTS[0], 588))

    def test_duplicate_trace_unknown_sdk_and_bad_total_rejected(self):
        packet = self.wire(query='')
        bad = [([(CLIENTS[0], packet)] * 2, {'200': 2}),
               ([('unknown-1', packet)], {'200': 1}), ([(CLIENTS[0], packet)], {'404': 1})]
        for rows, statuses in bad:
            with self.subTest(rows=rows, statuses=statuses), self.assertRaises(ValueError):
                index(self.catalog, [('trial', (590, rows, statuses))])

    def test_original_historical_wire_receipts(self):
        table, summary = render()
        records = list(csv.DictReader(io.StringIO(table)))
        self.assertEqual(len(records), summary['methods'])
        self.assertEqual(summary['source_requests'], summary['assigned_requests'] + len(summary['unassigned']))
        self.assertEqual(len(set((r['method'], r['path']) for r in records)), 231)
        plain = next(r for r in records if r['method'] == 'POST' and r['path'] == '/responses')
        beta = next(r for r in records if r['method'] == 'POST' and r['path'] == '/responses?beta=true')
        self.assertIn('vectors/200=1', plain['python-3.55.1'])
        self.assertFalse(any(beta[c] for c in CLIENTS))
        self.assertTrue(any(r['path'] == '/v1/responses' for r in summary['unassigned']))
