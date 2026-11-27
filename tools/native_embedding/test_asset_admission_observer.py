"""CPU controls for the live embedding/VAD overlap observation."""
import os
import tempfile
import unittest
from unittest.mock import patch

from tools.native_embedding.asset_admission_http import Stage, journal


class AssetObserverTests(unittest.TestCase):
    def setUp(self):
        self.stage = object.__new__(Stage)
        self.file = self.enterContext(tempfile.TemporaryFile())
        info = os.fstat(self.file.fileno())
        self.identity = (info.st_dev, info.st_ino)
        self.pid = os.getpid()
        self.start = journal.process_start(self.pid)

    def observe(self, *samples):
        with patch.object(self.stage, 'reader_health', side_effect=samples):
            return self.stage.observe_contention(self.pid, self.start, self.identity)

    @staticmethod
    def sample(vad, embedding='loading'):
        return dict(embedding=dict(state=embedding),
                    vad=dict(state=vad, load_attempts=int(vad != 'configured')))

    def test_waits_for_vad_to_enter_loading_with_same_open_inode(self):
        loading = self.sample('loading')
        self.assertEqual(self.observe(self.sample('configured'), loading), loading)

    def test_rejects_post_without_overlapping_backend_load(self):
        for sample in (self.sample('ready'), self.sample('degraded'),
                       self.sample('loading', embedding='ready')):
            with self.subTest(sample=sample), self.assertRaises(RuntimeError):
                self.observe(sample)

    def test_rejects_closed_weight_even_with_two_loading_states(self):
        self.file.close()
        with self.assertRaisesRegex(RuntimeError, 'weight reader finished'):
            self.observe(self.sample('loading'))

    def test_rejects_pid_reuse_even_with_the_same_inode(self):
        self.start -= 1
        with self.assertRaisesRegex(RuntimeError, 'weight reader finished'):
            self.observe(self.sample('loading'))
