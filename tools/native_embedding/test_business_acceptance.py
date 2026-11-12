"""Targeted native backend discovery and business acceptance negative controls."""
import copy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from tools.native_embedding import business_acceptance as stage


class NativeBusinessTests(unittest.TestCase):
    def test_discovery_schema_retains_historical_config_and_precise_native_contract(self):
        schema = stage.read(stage.ROOT/'docs/serve-config-v1.schema.json')
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        value = stage.read(stage.ROOT/'docs/evidence/serve-access-config-fix-20261109/observed-config.json')
        validator.validate(value)
        properties = schema['properties']['native_embedding']['properties']
        backend = {k:v['const'] for k,v in properties.items() if 'const' in v}
        backend['enabled'] = True
        health = dict(state='configured', reason='none', model=stage.MODEL,
            core_readiness_dependency=False, device_bytes=0, load_attempts=0,
            load_failures=0, recovery='request_driven', retry_delay_ms=5000)
        value.update(native_embedding=backend, native_embedding_health=health)
        validator.validate(value)
        for section, key, invalid in [
            ('native_embedding', 'dimensions', 2560),
            ('native_embedding', 'weight_sha256', '0'*64),
            ('native_embedding', 'enabled', 1),
            ('native_embedding', 'directory', '/private/path'),
            ('native_embedding_health', 'state', 'unknown'),
            ('native_embedding_health', 'core_readiness_dependency', True),
            ('native_embedding_health', 'load_attempts', -1),
            ('native_embedding_health', 'recovery', 'restart_model')]:
            bad = copy.deepcopy(value); bad[section][key] = invalid
            with self.subTest(section=section, key=key):
                self.assertFalse(validator.is_valid(bad))
        for section in ['native_embedding', 'native_embedding_health']:
            for key in value[section]:
                bad = copy.deepcopy(value); del bad[section][key]
                self.assertFalse(validator.is_valid(bad), (section, key))

    def test_fixed_requests_budget_and_trained_tokenizer_inputs(self):
        requests = stage.requests()
        self.assertEqual(stage.LIMITS, dict(starts=2, chats=9, controls=2000, seconds=900))
        self.assertEqual(stage.MAX_EMBEDDING_STEPS, (11+1+1+1+1)*30)
        self.assertEqual(5+sum(len(requests[k]) for k in ['batch','cancel'])+2, 9)
        for key, value in requests.items():
            if key == 'chat': continue
            for row in value if key in ['batch', 'cancel'] else [value]:
                self.assertEqual(stage.errors(row, 'embeddings_create'), [], key)
                self.assertEqual(row['model'], stage.MODEL)
        self.assertEqual(requests['ids']['input'], [[0,151643]])
        self.assertEqual(requests['reduced']['input'], [0,151643])
        self.assertEqual(len(requests['default']['input']), 11)
        self.assertTrue(all(s.startswith(stage.numerical.INSTRUCTION)
                            for s in requests['default']['input'][6:]))
        self.assertEqual(sum(map(len, requests['cancel'][0]['input'])), 16384)
        self.assertEqual(stage.Stage.__new__(stage.Stage).client_policy()['clients'][1]
                         ['tokens_per_minute'], 16)

    def test_retrieval_oracle_requires_all_actual_document_ranks(self):
        docs = [[float(i == j) for j in range(5)] for i in range(5)]
        rows = [docs[0]]+docs+copy.deepcopy(docs)
        self.assertEqual(len(stage.retrieval(rows)), 5)
        rows[7] = rows[6]
        with self.assertRaisesRegex(RuntimeError, 'retrieval failed'):
            stage.retrieval(rows)
        with self.assertRaisesRegex(RuntimeError, 'cardinality'):
            stage.retrieval(rows[:-1])

    def test_native_vector_oracle_rejects_wrong_model_and_wire(self):
        value = dict(object='list', model=stage.MODEL,
            data=[dict(object='embedding',index=0,embedding=[.6,.8])],
            usage=dict(prompt_tokens=2,total_tokens=2))
        self.assertEqual(stage.vectors(value,1,2), [[.6,.8]])
        for bad in [dict(value,model=stage.generic.foreground.MODEL),
                    dict(value,data=[dict(value['data'][0],index=1)]),
                    dict(value,usage=dict(prompt_tokens=2,total_tokens=3))]:
            with self.assertRaises(RuntimeError): stage.vectors(bad,1,2)


if __name__ == '__main__': unittest.main()
