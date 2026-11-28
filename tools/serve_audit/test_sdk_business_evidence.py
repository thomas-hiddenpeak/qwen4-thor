"""Prevent cross-method or cross-version promotion of incomplete wire evidence."""
import csv
import io
import json
from pathlib import Path
import unittest

from .sdk_business_evidence import Catalog, CLIENTS, index, render
from .stream_sdk_evidence import actor
from .transcription_sdk_evidence import transcription


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

    def test_stream_get_requires_original_query_identity_and_sdk(self):
        original = dict(method="GET", url="/v1/responses/resp_1?stream=true", version="6.34.0",
                        status=200, trace="req-stream")
        def read(wire):
            return actor(dict(id="resp_1", status="completed", sdk="node-6.34.0", requests=[wire]),
                         "node-6.34.0", "resp_1", "read")
        self.assertEqual(read(original)[1]["query"], "stream=true")
        for change in ({"version":"7.47.0"}, {"url":"/v1/responses/resp_1"},
                       {"url":"/v1/responses/resp_2?stream=true"}, {"status":404}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                read(original | change)

    def test_stream_evidence_preserves_sdk_and_serving_generation(self):
        table, manifest = render()
        rows = list(csv.DictReader(io.StringIO(table)))
        source = manifest["sources"]
        read = next(r for r in rows if r["path"] == "/responses/{response_id}" and r["method"] == "GET")
        self.assertTrue(all("stream-tls/200=1" in read[c] for c in CLIENTS))
        self.assertEqual(source["stream-tls"]["requests"], 7)
        self.assertEqual(source["stream-tls"]["direct_http_controls"], 2)
        old, new = (source["stream-gateway-" + phase] for phase in ("prepare", "restore"))
        self.assertGreater(new["generation"], old["generation"])
        self.assertNotEqual(old["manifest"], new["manifest"])
        self.assertEqual(new["manifest"], source["stream-model-restore"]["manifest"])
        self.assertIn("stream-gateway-restore/200=1", read["node-7.47.0"])
        self.assertNotIn("stream-gateway-restore", read["python-3.55.1"])

    def test_duplicate_trace_unknown_sdk_and_bad_total_rejected(self):
        packet = self.wire(query='')
        bad = [([(CLIENTS[0], packet)] * 2, {'200': 2}),
               ([('unknown-1', packet)], {'200': 1}), ([(CLIENTS[0], packet)], {'404': 1})]
        for rows, statuses in bad:
            with self.subTest(rows=rows, statuses=statuses), self.assertRaises(ValueError):
                index(self.catalog, [('trial', (590, rows, statuses))])

    def test_mixed_speech_requires_typed_source_and_actual_wire(self):
        folder = Path(__file__).resolve().parents[2] / 'docs/evidence/serve-transcription-discovery-20261128'
        capture = json.loads((folder / 'sdk-wire/device-transcription.json').read_bytes())
        witness = json.loads((folder / 'device.json').read_bytes())
        witness['trace'] = witness['authenticated_requests'][1]['trace']
        label, packet = transcription(capture, witness, witness['source_sha256'])
        self.assertEqual((label, packet['trace']), ('python-3.55.1', witness['trace']))
        for change in ({'method':'GET'}, {'path':'/v1/audio/translations'}, {'sdk':'3.42.1'},
                       {'trace':'another-request'}, {'source_sha256':'0'*64}, {'labels':['A','A']},
                       {'wire_sha256':'0'*64}, {'request':{'response_format':'json'}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                transcription(capture | change, witness, witness['source_sha256'])

    def test_mixed_speech_matrix_distinguishes_transports_and_generations(self):
        table, manifest = render()
        rows = list(csv.DictReader(io.StringIO(table)))
        speech = next(r for r in rows if r['path'] == '/audio/transcriptions' and r['method'] == 'POST')
        sources = {k:v for k,v in manifest['sources'].items() if k.startswith('speech-') and not k.startswith('speech-issued-')}
        self.assertEqual(len(sources), 7)
        self.assertEqual(sum(v['requests'] for v in sources.values()), 8)
        self.assertTrue(all(k+'/200=1' in speech['python-3.55.1'] for k in sources))
        self.assertTrue(all(all(k+'/200=' not in speech[c] for k in sources)
                            for c in CLIENTS if c != 'python-3.55.1'))
        self.assertEqual(sources['speech-tls']['direct_http_controls'], 1)
        self.assertTrue(sources['speech-capacity-primary']['mtp'])
        self.assertFalse(sources['speech-capacity-light']['mtp'])
        before, after = (sources['speech-gateway-'+p] for p in ('prepare','restore'))
        self.assertGreater(after['generation'], before['generation'])
        self.assertNotEqual(before['manifest'], after['manifest'])
        self.assertEqual(after['manifest'], sources['speech-model-restore']['manifest'])
        for client in CLIENTS:
            for mode in ('json', 'sse'):
                label = f'speech-issued-{client}-{mode}'
                self.assertIn(label+'/200=1', speech[client])
                meta = manifest['sources'][label]
                self.assertEqual((meta['sdk'], meta['mode'], meta['manifest']),
                                 (client, mode, before['manifest']))
                self.assertTrue(all(label+'/200=' not in speech[other] for other in CLIENTS if other != client))

    def test_published_matrix_matches_reconstruction(self):
        directory = Path(__file__).resolve().parents[2] / 'docs/evidence/serve-sdk-business-index-20261128'
        table, manifest = render()
        self.assertEqual((directory/'business.csv').read_bytes(), table.encode())
        self.assertEqual(json.loads((directory/'sources.json').read_bytes()), manifest)
        goals = json.loads((directory.parents[1] / 'serve-master-goals-v2.json').read_bytes())
        published = next(t for t in goals['tasks'] if t['id'] == 'A12')['sdk_business_evidence_20261128']
        for key in ('source_requests', 'assigned_requests', 'methods_with_success', 'method_sdk_pairs_with_success'):
            self.assertEqual(published[key], manifest[key], key)
        self.assertEqual(published['unassigned'], len(manifest['unassigned']))


    def test_batch_recovery_keeps_the_writer_and_reader_on_their_original_sides(self):
        table, manifest = render()
        rows = list(csv.DictReader(io.StringIO(table)))
        cancel = next(r for r in rows if r['method'] == 'POST' and r['path'].endswith('/cancel')
                      and r['path'].startswith('/batches/'))
        for incident in ('model', 'gateway'):
            with self.subTest(incident=incident):
                before = 'batch-' + incident + '-submit'
                after = 'batch-' + incident + '-restore'
                self.assertEqual(manifest['sources'][before]['phase'], 'before_incident')
                self.assertEqual(manifest['sources'][after]['phase'], 'after_recovery')
                initial, recovered = manifest['sources'][before], manifest['sources'][after]
                self.assertGreater(recovered['generation'], initial['generation'])
                self.assertEqual(initial['manifest'], recovered['initial_manifest'])
                self.assertEqual(initial['manifest'] == recovered['manifest'], incident == 'model')
                self.assertIn(before+'/429=1', cancel['python-3.55.1'])
                self.assertIn(after+'/409=1', cancel['node-7.47.0'])
                self.assertNotIn(after+'/', cancel['python-3.55.1'])
                self.assertNotIn(before+'/', cancel['node-7.47.0'])

    def test_original_historical_wire_receipts(self):
        table, summary = render()
        records = list(csv.DictReader(io.StringIO(table)))
        self.assertEqual(len(records), summary['methods'])
        self.assertEqual(summary['source_requests'], summary['assigned_requests'] + len(summary['unassigned']))
        self.assertEqual(len(set((r['method'], r['path']) for r in records)), 231)
        plain = next(r for r in records if r['method'] == 'POST' and r['path'] == '/responses')
        beta = next(r for r in records if r['method'] == 'POST' and r['path'] == '/responses?beta=true')
        self.assertIn('vectors/200=1', plain['python-3.55.1'])
        self.assertIn('responses/200=', beta['node-7.47.0'])
        self.assertIn('responses/200=', plain['python-2.8.1'])
        self.assertFalse(beta['python-2.8.1'])
        self.assertFalse(beta['node-6.34.0'])
        self.assertTrue(any(r['path'] == '/v1/responses' for r in summary['unassigned']))
