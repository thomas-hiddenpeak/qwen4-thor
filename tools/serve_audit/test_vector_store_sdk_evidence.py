"""Exercise attribution against the actual six-client short run and recovery exchanges."""
import copy
import json
import unittest
from pathlib import Path

from .sdk_business_evidence import CLIENTS, render
from .vector_store_sdk_evidence import device_calls, recovered_calls

DATA = Path(__file__).resolve().parents[2] / 'docs/evidence/serve-previous-page-20261128'


def read(name):
    return json.loads((DATA / name).read_bytes())


class VectorStoreEvidenceTest(unittest.TestCase):
    def test_device_changes_are_rejected(self):
        proof = read('device.json')
        raw = read('device-wire.json')
        docs = proof['document_sha256']
        def calls(record):
            return list(device_calls(record, proof, CLIENTS, docs, 606))
        self.assertEqual(sum(len(rows) for _, _, rows in calls(raw)), 132)
        for mutate in (
            lambda v:v['setup'].update(download_sha256='0'*64),
            lambda v:v['name_updates'][CLIENTS[0]].update(sdk=CLIENTS[1]),
            lambda v:v['clients'][CLIENTS[0]]['pages'][0]['ids'].reverse(),
            lambda v:v['setup']['requests'][11].update(method='DELETE'),
            lambda v:v['chat'].update(answer='608'),
            lambda v:v['cleanup']['requests'].pop(),
        ):
            changed = copy.deepcopy(raw)
            mutate(changed)
            with self.assertRaises(ValueError):
                calls(changed)

    def test_recovery_changes_are_rejected(self):
        delivery = read('delivery.json')
        old = read(delivery['previous_delivery'])
        docs = {k:old['sdk_evidence'][k] for k in
                ('cpu.json', 'device.json', 'capacity-business.json', 'capacity.json', 'managed.json')}
        raw = read('managed.json')
        def calls(record):
            return list(recovered_calls(record, delivery['publication'], docs))
        self.assertEqual([len(rows) for _,rows,_ in calls(raw)], [12]*4)
        for mutate in (
            lambda v:v['roles']['primary'].update(registered_fallback=delivery['publication']['primary']),
            lambda v:v['roles']['fallback'].update(new_generation=v['roles']['fallback']['old_generation']),
            lambda v:v['roles']['primary']['requests']['restore']['requests'][0].update(sdk=True),
            lambda v:v['roles']['primary']['requests']['prepare']['requests'][-1].update(scope='owner'),
            lambda v:v['roles']['fallback']['requests']['restore']['requests'][-1]['response'].update(has_more=True),
        ):
            changed = copy.deepcopy(raw)
            mutate(changed)
            with self.assertRaises(ValueError):
                calls(changed)

    def test_matrix_preserves_the_actual_sdk_and_serving_generation(self):
        _, manifest = render()
        sources = manifest['sources']
        for client in CLIENTS:
            row = sources['vector-store-pages-and-name-' + client]
            self.assertEqual((row['sdk'], row['requests'], row['registered_during_run']), (client, 16, 608))
            self.assertTrue(row['tested_before_registration'])
        for role in ('primary','fallback'):
            before, after = (sources['vector-store-' + role + '-' + phase] for phase in ('prepare','restore'))
            for row in (before, after):
                self.assertEqual((row['sdk'],row['requests'],row['direct_http_controls']), ('python-3.55.1',12,1))
            self.assertLess(before['generation'],after['generation'])
        self.assertEqual(sources['vector-store-primary-restore']['manifest'],
                         sources['vector-store-fallback-restore']['manifest'])
        self.assertNotEqual(sources['vector-store-primary-prepare']['manifest'],
                            sources['vector-store-primary-restore']['manifest'])
