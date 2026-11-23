"""Prevent an incomplete prior business run from becoming false acceptance."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools.serve_live import client_acceptance as driver


class RetainedQuery(unittest.TestCase):
    def setUp(self):
        # The public, credential-free original is sufficient on a clean
        # checkout. Do not require the private developer acceptance directory.
        work = driver.ROOT / '.q4t-work'
        work.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name)
        public = driver.ROOT / 'docs/evidence/serve-live-close-tail-failed-20261121'
        index = json.loads((public / 'index.json').read_text())
        names = ('query-fixture.json', 'query-speech.http.json', 'query.pcm',
                 'query-speech.request.json', 'fixture-before.json',
                 'fixture-drained.json', 'client.events.jsonl', 'result.json')
        with tarfile.open(public / 'evidence.tar.gz', 'r:gz') as archive:
            for name in names:
                member = 'original/' + name
                raw = archive.extractfile(member).read(8 * 1024 * 1024 + 1)
                self.assertLessEqual(len(raw), 8 * 1024 * 1024)
                self.assertEqual(hashlib.sha256(raw).hexdigest(),
                                 index[member]['sha256'])
                (self.source / name).write_bytes(raw)
        # The original public archive stores one copy of identical PCM bytes.
        (self.source / 'query-speech.wire').write_bytes(
            (self.source / 'query.pcm').read_bytes())

    def replace_json(self, name, change):
        original = driver.read

        def read(path):
            value = original(path)
            if Path(path).name == name:
                value = deepcopy(value)
                change(value)
            return value

        return patch.object(driver, 'read', side_effect=read)

    def test_real_partial_success_keeps_original_failure(self):
        proof = driver.retained_query(self.source)
        self.assertEqual(proof['original_phase_status'], 'FAIL')
        self.assertEqual(proof['synthesis_and_transcription'], 'PASS')
        self.assertFalse(proof['transcript_supplied_to_asr'])
        self.assertEqual(proof['bytes'], 138240)
        self.assertNotIn('speech_fixture', driver.LIMITS)

    def test_changed_waveform_cannot_reuse_prior_receipt(self):
        original = driver.sha
        with patch.object(driver, 'sha', side_effect=lambda p:
                          '0' * 64 if Path(p).name == 'query.pcm' else original(p)):
            with self.assertRaises(RuntimeError):
                driver.retained_query(self.source)

    def test_missing_actual_settlement_is_not_synthesis_proof(self):
        with self.replace_json('fixture-drained.json', lambda value:
                               value.update({'q4t_client_rate_admitted_total{client="beta"}': 0})):
            with self.assertRaises(RuntimeError):
                driver.retained_query(self.source)

    def test_transcript_prompt_cannot_pass_audio_only_qualification(self):
        with self.replace_json('query-fixture.json', lambda value:
                               value.update(transcript_supplied_to_asr=True)):
            with self.assertRaises(RuntimeError):
                driver.retained_query(self.source)

    def test_original_phase_cannot_be_relabelled_pass(self):
        with self.replace_json('result.json', lambda value:
                               value.update(status='PASS')):
            with self.assertRaises(RuntimeError):
                driver.retained_query(self.source)


if __name__ == '__main__':
    unittest.main()
