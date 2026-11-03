"""Operational compatibility follows the observed v3/v4 server contract."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tools.serve_ops import contracts, diagnose, fixtures, identity, rules, transport
from tools.serve_ops.test_ops import instance


class VersionedOperations(unittest.TestCase):
    def snapshot(self,version,mutation=None):
        protocol=fixtures.configuration()['protocol_source_sha256']
        def fetch(port,path,**kw):
            value=fixtures.reply(port,path,version=version,**kw)
            if mutation:mutation(path,value)
            return value
        with patch.object(identity,'release_identity',return_value=dict(binary_sha256='a'*64,protocol_sha256=protocol)), \
             patch.object(identity,'process',side_effect=[instance(),instance()]), \
             patch.object(transport,'get',side_effect=fetch):
            return diagnose.snapshot(18107,12345,Path('unused'))

    def test_v3_and_v4_are_explicit_and_complete(self):
        for version,expected in ((3,87),(4,92)):
            report=self.snapshot(version)
            self.assertEqual(report['state'],'HEALTHY')
            self.assertEqual(report['observability_version'],version)
            self.assertEqual(len(report['metrics']),expected)
            self.assertEqual(report['metrics']['q4t_business_connections'],1 if version==3 else 0)
        self.assertEqual(self.snapshot(4)['configuration']['connection_handlers'],136)

    def test_v4_cannot_silently_downgrade_missing_metric_or_configuration(self):
        def metric(path,value):
            if path=='/metrics':value['body']=value['body'].replace('q4t_observation_connections 1\n','')
        def config(path,value):
            if path=='/v1/config':
                doc=json.loads(value['body']);doc['limits'].pop('observation_connections');value['body']=json.dumps(doc)
        for mutation in (metric,config):self.assertEqual(self.snapshot(4,mutation)['state'],'UNKNOWN')

    def test_unknown_version_or_forged_partition_rejected(self):
        def mutation(path,value):
            if path=='/v1/capabilities':
                doc=json.loads(value['body']);doc['http']['observation_reserved_connections']=5
                value['body']=json.dumps(doc)
        self.assertEqual(self.snapshot(4,mutation)['state'],'UNKNOWN')
        self.assertEqual(self.snapshot(5)['state'],'UNKNOWN')

    def test_new_counter_difference_and_rejection_finding(self):
        before=self.snapshot(4);after=copy.deepcopy(before);after['monotonic']+=2
        key='q4t_connection_rejections_total{kind="observation"}'
        after['metrics'][key]+=3
        after['comparison']=diagnose.compare(before,after)
        self.assertEqual(after['comparison']['counters'][key],3)
        findings=diagnose.findings(after)
        item=next(r for r in findings if r['code']=='admission_rejections_observed')
        self.assertEqual(item['evidence'],{key:3})
        before['observability_version']=3
        self.assertEqual(diagnose.compare(before,after)['state'],'unavailable')

    def test_invalid_owners_or_labels_fail_closed(self):
        values=fixtures.metrics(version=4);values['q4t_observation_connections']=5
        with self.assertRaises(contracts.InvalidObservation):
            contracts.parse_metrics(fixtures.text_metrics(values),version=4)
        values=fixtures.metrics(version=4)
        values['q4t_connection_rejections_total{kind="private"}']=1
        with self.assertRaises(contracts.InvalidObservation):
            contracts.parse_metrics(fixtures.text_metrics(values),version=4)

    def test_legacy_policy_retains_accepted_exact_bytes(self):
        import hashlib
        raw=(json.dumps(rules.render(version=3),indent=2)+'\n').encode()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
            '77a4ddc613b002508be336ab41dfae17c9d9e6421f1638c15ccbfcca0eed637e')


if __name__=='__main__':unittest.main()
