"""Check that capacity qualification cannot transfer a different recording receipt."""
import copy
import unittest

from tools.serve_live import recording_profiles as recording
from tools.serve_service import package


class RecordingProfiles(unittest.TestCase):
    def test_each_observed_capacity_and_its_counterexamples(self):
        proof = package.read(recording.PROOF)
        release = package.read(recording.RELEASE)['publication']['primary']
        for name, observed in proof['profiles'].items():
            plan = dict(binary_sha256=proof['engine'], ingress_sha256=proof['ingress'],
                        selected_sha256=release, capacity=observed['capacity'],
                        recording_download_handoff=dict(release=release))
            recording.check(proof, name, plan)
            changes = [
                (('engine',), '0' * 64),
                (('profiles', name, 'foreign_download'), 200),
                (('profiles', name, 'sdk'), observed['sdk'][1:]),
                (('profiles', name, 'sdk', 0, 'trace'), observed['sdk'][1]['trace']),
                (('profiles', name, 'sdk', 0, 'sha256'), '0' * 64),
                (('profiles', name, 'capacity', 'mtp_enabled'), not observed['capacity']['mtp_enabled']),
                (('profiles', name, 'direct_401', 'status'), 200),
                (('final', 'default_unchanged'), False),
                (('real_recording', 'original_store_unchanged'), False),
            ]
            for keys, value in changes:
                with self.subTest(name=name, field=keys):
                    candidate = copy.deepcopy(proof)
                    target = candidate
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = value
                    with self.assertRaises(RuntimeError):
                        recording.check(candidate, name, plan)
