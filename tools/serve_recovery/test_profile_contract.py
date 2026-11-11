"""CPU qualification contract; synthetic light observation is NOT model proof."""
import copy
import json
from pathlib import Path
import unittest

from tools.serve_ops import contracts
from tools.serve_recovery import ledger, profile_contract as contract
from tools.serve_service import capacity, package

ROOT = Path(__file__).resolve().parents[2]


class ProfileContractTests(unittest.TestCase):
    def setUp(self):
        root = ROOT/'docs/evidence/serve-native-embedding-business-20261111'
        self.config = json.loads((root/'config-1.json').read_text())
        self.caps = json.loads((root/'capabilities-1.json').read_text())
        delivered = ROOT/'docs/evidence/serve-native-embedding-delivery-20261111/candidate/bundle'
        self.base = json.loads((delivered/'engine/manifest.json').read_text())
        self.bundle = json.loads((delivered/'manifest.json').read_text())
        self.primary = capacity.normalize(dict(version=1, max_tokens=384))
        self.pair = contract.profiles(self.primary)
        self.fixed = contract.binding(self.base['files']['q4t'],
            self.base['protocol_sha256'], self.base['model_files'],
            self.bundle['model_assets'], self.base['settings']['native_embedding'])
        self.observed = contracts.configuration(self.config, version=5)
        self.memory = self.config['startup_memory']['checks']['before_allocations']

    def fixture(self):
        # Only validate representation. No runtime/packaging test may use this
        # as evidence of a real light model: it intentionally has no artifact.
        caps = copy.deepcopy(self.caps); caps['runtime']['mtp_enabled'] = False
        observed = dict(self.observed, mtp_enabled=False, mtp_k=None)
        memory = dict(self.memory, estimate_bytes=self.memory['estimate_bytes']-1,
                      required_bytes=self.memory['required_bytes']-1)
        records = dict(primary=contract.qualification('primary', self.primary,
            self.fixed,self.caps,self.observed,self.memory,'a'*64),
            light=contract.qualification('light', self.pair['light'],self.fixed,
                caps,observed,memory,'b'*64))
        return dict(format=contract.FORMAT,identity=self.fixed,profiles=self.pair,
                    qualifications=records)

    def test_light_profile_cannot_reduce_service_contract(self):
        self.assertEqual(contract.profile_pair(self.pair), self.pair)
        for field in ['max_len','max_seq','max_prefill','max_tokens',
                      'max_inflight_requests','max_queued_requests',
                      'request_deadline_ms','mtp_k']:
            bad = copy.deepcopy(self.pair);bad['light'][field] += 1
            with self.subTest(field=field), self.assertRaises((ValueError,RuntimeError)):
                contract.profile_pair(bad)
        for enabled in [False,0,'true']:
            with self.assertRaises((ValueError,RuntimeError)):
                contract.profiles(dict(self.primary,mtp_enabled=enabled))

    def test_capability_equivalence_does_not_hide_media_tools_or_embedding_loss(self):
        light = copy.deepcopy(self.caps);light['runtime']['mtp_enabled'] = False
        self.assertEqual(len(contract.capability_pair(self.caps,light)),64)
        for key in ['structured_output','embeddings','media','responses']:
            bad = copy.deepcopy(light);bad.pop(key)
            with self.assertRaisesRegex(ValueError,'application capabilities'):
                contract.capability_pair(self.caps,bad)
        with self.assertRaisesRegex(ValueError,'MTP capability'):
            contract.capability_pair(self.caps,self.caps)

    def test_actual_primary_configuration_and_memory_are_bound(self):
        contract.configuration(self.primary,self.observed)
        self.assertGreater(contract.startup_memory(self.memory),0)
        for key,value in [('max_context_tokens',4096),('default_output_tokens',256),
                          ('mtp_k',True),('structured_backend',False)]:
            with self.assertRaises((ValueError,RuntimeError)):
                contract.configuration(self.primary,dict(self.observed,**{key:value}))
        for key,value in [('estimate_bytes',None),('required_bytes',True),
                          ('required_bytes',self.memory['required_bytes']-1),
                          ('observed_available_bytes',0),('state','rejected')]:
            with self.assertRaises(ValueError):
                contract.startup_memory(dict(self.memory,**{key:value}))

    def test_asset_binding_keeps_all_fixed_models_and_rejects_unsafe_paths(self):
        native = self.base['settings']['native_embedding']
        relocated = dict(native,directory='/another/assets')
        self.assertEqual(contract.binding(self.base['files']['q4t'],
            self.base['protocol_sha256'], self.base['model_files'],
            self.bundle['model_assets'],relocated), self.fixed)
        self.assertEqual(contract.identity(self.fixed),self.fixed)
        for name in ['../weights.safetensors','/weights','a//b','a/./b','a\0b']:
            bad = copy.deepcopy(self.fixed)
            bad['model_assets'][name] = next(iter(bad['model_assets'].values()))
            with self.assertRaises(ValueError):contract.identity(bad)
        bad = copy.deepcopy(self.fixed);bad['native_embedding']['model']='different'
        with self.assertRaises((ValueError,RuntimeError)):contract.identity(bad)

    def test_receipts_reject_unqualified_capacity_assets_and_single_evidence(self):
        value = self.fixture();contract.validate(value)
        for field,replacement in [('capacity_sha256','0'*64),('identity_sha256','0'*64),
            ('capabilities_sha256','0'*64),('evidence_sha256','a'*64),
            ('startup_required_bytes',value['qualifications']['primary']['startup_required_bytes'])]:
            bad = copy.deepcopy(value);bad['qualifications']['light'][field]=replacement
            with self.subTest(field=field),self.assertRaises(ValueError):contract.validate(bad)
        bad = copy.deepcopy(value);bad['identity']['native_embedding']=None
        with self.assertRaisesRegex(ValueError,'identity differs'):contract.validate(bad)
        bad = copy.deepcopy(value);bad['qualifications'].pop('light')
        with self.assertRaises(ValueError):contract.validate(bad)

    def test_pair_digest_binds_source_identity_and_only_explicit_fields(self):
        value = self.fixture();before=copy.deepcopy(value)
        digest = contract.digest(value)
        self.assertEqual(value,before)
        self.assertEqual(digest,ledger.digest(value))
        bad = copy.deepcopy(value);bad['ignored_extra']='discard capabilities'
        with self.assertRaises(ValueError):contract.validate(bad)
        caps=copy.deepcopy(self.caps);caps['protocol_source_sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'protocol observation'):
            contract.qualification('primary',self.primary,self.fixed,caps,
                                   self.observed,self.memory,'a'*64)


if __name__=='__main__':unittest.main()
