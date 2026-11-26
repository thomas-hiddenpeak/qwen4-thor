from copy import deepcopy
import unittest
from unittest.mock import patch

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import chatkit_actions_profiles as handoff
from tools.serve_responses import video_track_grid_profiles as grid


class TrackGridProfiles(unittest.TestCase):
    def setUp(self):
        ids = {name:'resp_' + str(i)*32 for i, name in enumerate(('uploaded','inline','replay'), 1)}
        hashes = {name:str(i)*64 for i, name in enumerate(ids, 1)}
        parent = ids['uploaded']
        original = dict(saved={'response':parent}, sdk='3.55.1', resources=hashes,
                        responses={name:dict(response=rid) for name,rid in ids.items()})
        self.source = original
        self.reader = patch.object(grid.package, 'read', return_value={'sdk':original})
        self.reader.start()
        self.addCleanup(self.reader.stop)
        history = {name:dict(response=rid, sha256=hashes[name], peer=404,
                             previous=parent if name=='replay' else None)
                   for name,rid in ids.items()}
        self.first = dict(sdk=original['sdk'], history=history, deleted=404, restored=None,
                          saved=dict(response='resp_'+'4'*32, previous=parent, sha256='4'*64, peer=404),
                          colors={'top_right':'green','bottom_right':'yellow'}, independent_chat='42',
                          events=['response.created','response.output_text.delta','response.completed'])
        self.second = deepcopy(self.first)
        self.second['restored'] = deepcopy(self.first['saved'])
        self.second['saved'].update(response='resp_'+'5'*32, sha256='5'*64)

    def test_handoff_registers_program_profile_predecessor(self):
        spec = handoff.handoff({grid.KEY:{}})
        self.assertIs(spec.check, grid.check)
        self.assertEqual((spec.receipt,spec.predecessor), (reuse.GRID_RECEIPT,reuse.FORK_RECEIPT))

    def test_both_profiles_read_originals_and_generate_distinct_answers(self):
        grid.check(self.first)
        grid.check(self.second,self.first)

    def test_all_originals_remain_scoped_and_byte_identical(self):
        for label in self.first['history']:
            for name,value in [('sha256','e'*64),('peer',200),('response','resp_other'),
                               ('previous','resp_wrong_parent')]:
                changed = deepcopy(self.first)
                changed['history'][label][name] = value
                with self.subTest(label=label, field=name), self.assertRaises(RuntimeError):
                    grid.check(changed)

    def test_replay_cannot_reuse_old_id_or_another_profile_answer(self):
        for path, value in [(('saved','response'),self.first['saved']['response']),
                            (('saved','previous'),self.first['history']['inline']['response']),
                            (('restored','sha256'),'d'*64), (('restored','peer'),200),
                            (('saved','response'),self.first['history']['replay']['response'])]:
            changed = deepcopy(self.second)
            changed[path[0]][path[1]] = value
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                grid.check(changed,self.first)

    def test_frozen_input_and_stream_have_observable_outcomes(self):
        for field,value in [('colors',{'top_left':'red'}),('deleted',200),('independent_chat',''),
                            ('events',['response.created','response.completed']),
                            ('events',['response.completed',*self.first['events']]),
                            ('restored',self.first['saved'])]:
            changed = dict(self.first, **{field:value})
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                grid.check(changed)

    def test_discovery_preserves_issued_clipping_behavior(self):
        prev = dict(native_video=dict(region_clipping={'frozen_conversion':'q4t.cpu_video_png.v7'},
                                      detail={'high_min_frame_pixels':65536}))
        current = reuse.track_grid_file_inputs(prev)
        self.assertNotIn('track_display_size',prev['native_video'])
        self.assertEqual(current['native_video']['region_clipping'],prev['native_video']['region_clipping'])
        self.assertTrue(current['native_video']['track_display_size']['clips_optional'])
        for wrong in [current, {'native_video':{}}, {'native_video':{'region_clipping':{}}}]:
            with self.assertRaises(ValueError):
                reuse.track_grid_file_inputs(wrong)


if __name__ == '__main__':
    unittest.main()
