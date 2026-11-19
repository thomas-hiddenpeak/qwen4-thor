"""CPU identity counterexamples for lossless managed SDK plan compaction."""
import copy
import unittest

from tools.native_asr import service_acceptance as service


def plan():
    directory = service.ROOT/'.q4t-work/frozen-sdk-cpu-fixture'
    files = {'package-lock.json': 'a'*64,
             'node_modules/openai/index.mjs': 'b'*64,
             'node_modules/ws/wrapper.mjs': 'c'*64}
    return dict(sdk=dict(sdk_directory=str(directory), files=files),
        sources={str(directory/name):digest for name, digest in files.items()})


class Binding(unittest.TestCase):
    def test_complete_files_preserved_once_in_frozen_sources(self):
        value = plan(); original = copy.deepcopy(value['sources'])
        service.compact_sdk(value)
        self.assertNotIn('files', value['sdk'])
        self.assertEqual(value['sources'], original)
        self.assertEqual(value['sdk']['files_binding']['files'], 3)
        service.verify_sdk_binding(value)

    def test_missing_changed_or_extra_module_cannot_be_compacted(self):
        for change in (lambda v:v['sources'].pop(next(iter(v['sources']))),
                       lambda v:v['sources'].update({next(iter(v['sources'])):'f'*64}),
                       lambda v:v['sources'].update({v['sdk']['sdk_directory']+'/extra.mjs':'f'*64})):
            value = plan(); change(value)
            with self.assertRaises(RuntimeError): service.compact_sdk(value)
        value = plan(); value['sdk']['sdk_directory'] += '/../other'
        with self.assertRaises(RuntimeError): service.compact_sdk(value)

    def test_bound_inventory_mutation_and_duplicate_claim_reject(self):
        for change in (lambda v:v['sources'].pop(next(iter(v['sources']))),
                       lambda v:v['sources'].update({next(iter(v['sources'])):'f'*64}),
                       lambda v:v['sdk']['files_binding'].update(files=2),
                       lambda v:v['sdk'].update(files={})):
            value = plan(); service.compact_sdk(value); change(value)
            with self.assertRaises(RuntimeError): service.verify_sdk_binding(value)


if __name__ == '__main__':
    unittest.main()
