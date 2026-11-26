import json
from pathlib import Path
import tempfile
import unittest

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import cache_retention_profiles as cache
from tools.serve_responses import chatkit_actions_profiles as handoff
from tools.serve_service import package


class CacheRetentionHandoff(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def capture(self, version):
        spelling = 'in-memory' if version == cache.LEGACY else 'in_memory'
        saved = dict(id='resp_1', status='completed', prompt_cache_retention=spelling,
                     metadata={'prompt_cache_retention':'in_memory'}, prompt_cache_options={'ttl':'30m'})
        events = [dict(sequence_number=0, type='response.created', response=dict(saved,status='in_progress')),
                  dict(sequence_number=1, type='response.completed', response=saved)]
        self.write('resource', saved)
        self.write('replay', events)
        return dict(sdk=version, id=saved['id'], foreign=404, get_sha256=package.sha(self.root/'resource.json'),
                    retention=spelling, cache_options=saved['prompt_cache_options'], events=len(events))

    def write(self, name, data):
        (self.root/(name+'.json')).write_text(json.dumps(data,ensure_ascii=False))

    def test_versions_have_the_same_canonical_history(self):
        old = self.capture(cache.LEGACY)
        first = cache.read_capture(self.root,old)
        new = self.capture(cache.CURRENT)
        self.assertNotEqual(new['get_sha256'],old['get_sha256'])
        self.assertEqual(first,cache.read_capture(self.root,new))

    def test_changed_event_or_user_metadata_is_detected(self):
        for field,value in [('id','resp_2'),('prompt_cache_retention','in_memory')]:
            reply = self.capture(cache.LEGACY)
            events = package.read(self.root/'replay.json')
            events[0]['response'][field] = value
            self.write('replay',events)
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                cache.read_capture(self.root,reply)
        reply = self.capture(cache.CURRENT)
        saved = package.read(self.root/'resource.json')
        saved['metadata']['prompt_cache_retention'] = 'in-memory'
        self.write('resource',saved)
        reply['get_sha256'] = package.sha(self.root/'resource.json')
        with self.assertRaises(RuntimeError):
            cache.read_capture(self.root,reply)

    def test_typed_timestamp_must_equal_the_raw_get(self):
        reply = self.capture(cache.CURRENT)
        saved = package.read(self.root/'resource.json')
        saved['completed_at'] = 1
        self.write('resource',saved)
        reply['get_sha256'] = package.sha(self.root/'resource.json')
        events = package.read(self.root/'replay.json')
        for value in (1,1.0):
            events[-1]['response']['completed_at'] = value
            self.write('replay',events)
            result = cache.read_capture(self.root,reply)
            if type(value) is int:
                base = result
            else:
                self.assertEqual(result,base)
        for value in (True,1.5,'1'):
            events[-1]['response']['completed_at'] = value
            self.write('replay',events)
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                cache.read_capture(self.root,reply)

    def test_incomplete_journal_and_cross_customer_access_are_detected(self):
        reply = self.capture(cache.LEGACY)
        for bad in [dict(reply,foreign=200),dict(reply,get_sha256='0'*64),dict(reply,events=1)]:
            with self.assertRaises(RuntimeError):
                cache.read_capture(self.root,bad)
        events = package.read(self.root/'replay.json')
        events[-1]['sequence_number'] = 3
        self.write('replay',events)
        with self.assertRaises(RuntimeError):
            cache.read_capture(self.root,reply)

    def test_receipt_extends_the_preceding_capacity(self):
        spec = handoff.handoff({cache.KEY:{}})
        self.assertEqual((spec.receipt,spec.predecessor), (reuse.CACHE_RETENTION_RECEIPT,reuse.LAYER_RECEIPT))
        self.assertEqual(reuse.REUSE_FORMAT[reuse.CACHE_RETENTION_RECORD],reuse.CACHE_RETENTION_FORMAT)
        self.assertIs(spec.check,cache.check)


if __name__ == '__main__':
    unittest.main()
