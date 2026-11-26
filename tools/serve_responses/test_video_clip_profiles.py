"""Exercise handoff identity, colors, stream completion and declaration checks."""
from copy import deepcopy
import unittest

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import video_clip_profiles as clips
from tools.serve_service import package


class ClipProfilesTest(unittest.TestCase):
    def setUp(self):
        row = deepcopy(package.read(clips.PROOF)['sdk'])
        first = row['responses']['uploaded']
        first['colors'] = {'top_left': 'red'}
        row['saved'] = dict(response=first['response'], file='file_'+'1'*32,
                            resource_sha256='1'*64, get=200, foreign=404, deleted=404)
        self.first = row
        self.second = deepcopy(row)
        replies = self.second['responses']
        replies['restored'] = deepcopy(replies['replay'])
        for index,reply in enumerate(replies.values(), 1):
            reply['response'] = 'resp_' + str(index)*32
        self.second['saved'] = dict(row['saved'], response=replies['uploaded']['response'],
                                    file='file_'+'2'*32, resource_sha256='2'*64)
        self.second['restored'] = deepcopy(row['saved'])

    def test_partial_answer_and_data_survive_the_switch(self):
        clips.check(self.first)
        clips.check(self.second,self.first)
        for key,value in [('file',self.second['saved']['file']),('resource_sha256','2'*64),
                          ('response',self.second['saved']['response']),('foreign',200),('deleted',200)]:
            row = deepcopy(self.second)
            row['restored'][key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                clips.check(row,self.first)

    def test_missing_scope_and_reused_ids_fail(self):
        for key in ('get','deleted','foreign'):
            row = deepcopy(self.first)
            row['saved'][key] = 403
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                clips.check(row)
        row = deepcopy(self.second)
        row['responses']['restored']['response'] = self.first['responses']['replay']['response']
        with self.assertRaises(RuntimeError):
            clips.check(row,self.first)

    def test_replay_requires_colors_absent_from_earlier_text(self):
        row = deepcopy(self.first)
        row['responses']['uploaded']['colors']['bottom_left'] = 'blue'
        with self.assertRaises(RuntimeError):
            clips.check(row)
        for key in ('replay','restored'):
            row = deepcopy(self.second)
            row['responses'][key]['colors']['bottom_left'] = 'black'
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                clips.check(row,self.first)

    def test_clipped_inline_and_stream_order_are_checked(self):
        for change in ('count','source','right','event','terminal'):
            row=deepcopy(self.first)
            if change=='count':row['counted'] += 1
            if change=='source':row['sources'].reverse()
            if change=='right':row['responses']['inline']['colors']['bottom_left']='blue'
            if change=='event':row['responses']['replay']['events'][2][0] += 1
            if change=='terminal':row['responses']['replay']['events'][-1][1]='response.failed'
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                clips.check(row)

    def test_additive_capability_preserves_the_prior(self):
        prior={'native_video':{'model_media_enabled':True},'chat_pdf':True}
        upgraded=reuse.clipped_file_inputs(prior)
        self.assertNotIn('rectangular_clipping',prior['native_video'])
        self.assertEqual(upgraded,dict(prior,native_video=dict(prior['native_video'],
            rectangular_clipping=reuse.CLIP_CAPABILITY)))
        with self.assertRaises(reuse.ledger.LedgerError):
            reuse.clipped_file_inputs(upgraded)
