"""Offline regressions: secrets and failed evidence cannot become acceptance."""
import json
import tarfile
import unittest

from tools.serve_deploy import report as r


class SecretFilter(unittest.TestCase):
    def test_verifier_literal_is_not_a_private_key(self):
        r.no_secrets('verifier.py', b"header = b'-----BEGIN PRIVATE KEY-----\\n'", [])

    def test_pem_variants_are_rejected(self):
        for kind in (b'PRIVATE', b'RSA PRIVATE', b'EC PRIVATE', b'ENCRYPTED PRIVATE'):
            with self.subTest(kind=kind), self.assertRaisesRegex(RuntimeError, 'secret'):
                r.no_secrets('private.pem', b'-----BEGIN ' + kind + b' KEY-----\nbody\n', [])

    def test_credentials_are_rejected_outside_pem(self):
        with self.assertRaisesRegex(RuntimeError, 'secret'):
            r.no_secrets('record.json', b'{"key":"test-secret-value"}', ['test-secret-value'])


class Evidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tarfile.open(r.ARCHIVE / 'records.tar.gz', 'r:gz') as archive:
            cls.records = {entry.name: archive.extractfile(entry).read() for entry in archive}

    def altered(self, name, change):
        value = json.loads(self.records[name])
        change(value)
        records = dict(self.records, **{name: json.dumps(value).encode()})
        return records.__getitem__

    def test_actual_failed_fixture_is_not_memory_stop_acceptance(self):
        value = r.audit(self.records.__getitem__)
        self.assertEqual(value['status'], 'NO_GO_MEMORY_STOP_COMBINATION')
        self.assertFalse(value['memory_stop_accepted'])
        self.assertEqual(value['validated_shutdown_streams'], 0)
        self.assertEqual(len(value['responses']), 4)
        self.assertEqual(value['incomplete_streams']['active']['outcome'],
                         'REQUEST_OUTCOME_UNKNOWN')

    def test_success_label_cannot_override_failure(self):
        read = self.altered('execution/result.json',
                            lambda v: v.update(status='PASS_BOUNDED_DEPLOYMENT'))
        with self.assertRaisesRegex(RuntimeError, 'stage result'):
            r.audit(read)

    def test_manual_stop_cannot_claim_memory_signal(self):
        read = self.altered('execution/managed-run/terminal.json',
                            lambda v: v['worker']['memory'].update(signal_sent=True))
        with self.assertRaisesRegex(RuntimeError, 'manual cleanup'):
            r.audit(read)

    def test_truncated_stream_cannot_gain_done(self):
        name = 'execution/business/queued-wire.bin'
        records = dict(self.records, **{name: self.records[name] + b'data: [DONE]\n\n'})
        with self.assertRaisesRegex(RuntimeError, 'unexpectedly complete'):
            r.audit(records.__getitem__)

    def test_failure_reason_cannot_be_erased(self):
        name = 'execution/pressure.jsonl'
        rows = [json.loads(line) for line in self.records[name].splitlines()]
        rows[-1]['error'] = None
        records = dict(self.records, **{
            name: ''.join(json.dumps(row) + '\n' for row in rows).encode()})
        with self.assertRaisesRegex(RuntimeError, 'pressure helper cleanup'):
            r.audit(records.__getitem__)


if __name__ == '__main__':
    unittest.main()
