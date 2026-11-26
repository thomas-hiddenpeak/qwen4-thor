from copy import deepcopy
import unittest
from unittest.mock import patch

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import chatkit_actions_profiles as handoff
from tools.serve_responses import video_layer_profiles as layers


class VideoLayerHandoff(unittest.TestCase):
    def test_discovery_adds_only_composition_to_track_grid(self):
        previous = {'native_video': {
            'track_display_size': {'frozen_conversion': 'q4t.cpu_video_png.v8'},
            'shared_work_budget': True}}
        after = reuse.layer_file_inputs(previous)
        native = dict(after['native_video'])
        self.assertEqual(native.pop('active_track_composition'), {
            'alternate_group': 'lowest-enabled-id', 'background': 'black',
            'frozen_conversion': 'q4t.cpu_video_png.v9', 'graphics_mode': 'copy',
            'layer_order': 'lower-in-front', 'timeline': 'movie'})
        self.assertEqual(native, previous['native_video'])
        for wrong in (after, {'native_video': {}}, {'native_video': {'track_display_size': {}}}):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                reuse.layer_file_inputs(wrong)

    def test_handoff_requires_interrupted_retry_predecessor(self):
        spec = handoff.handoff({layers.KEY: {}})
        self.assertIs(spec.check, layers.check)
        self.assertEqual((spec.receipt, spec.predecessor),
                         (reuse.LAYER_RECEIPT, reuse.INTERRUPTED_RETRY_RECEIPT))
        self.assertEqual(reuse.REUSE_FORMAT[reuse.LAYER_RECORD], reuse.LAYER_FORMAT)

    def test_late_frame_and_original_parent_survive_handoff(self):
        ids = {name: 'resp_' + str(i) * 32 for i, name in enumerate(('uploaded', 'inline', 'replay'), 1)}
        hashes = {name: str(i) * 64 for i, name in enumerate(ids, 1)}
        parent = ids['uploaded']
        sdk = dict(sdk='3.55.1', saved={'response': parent}, resources=hashes,
                   responses={label: {'response': rid} for label, rid in ids.items()})
        original = {name: dict(response=rid, sha256=hashes[name], peer=404,
                               previous=parent if name == 'replay' else None) for name, rid in ids.items()}
        first = dict(sdk=sdk['sdk'], history=original, deleted=404, restored=None,
                     colors=dict(first_bottom_left='red', last_bottom_right='black'), independent_chat='42',
                     saved=dict(response='resp_first', previous=parent, sha256='4'*64, peer=404),
                     events=['response.created', 'response.output_text.delta', 'response.output_text.delta',
                             'response.output_text.done', 'response.completed'],
                     stream=dict(response='resp_first', item='msg_first', output_index=0, events=5,
                                 deltas=2, text_sha256='d'*64))
        first['checkpoint'] = dict(response='resp_first', resource_sha256='4'*64, item='msg_first',
                                   output_index=0, text_sha256='d'*64, suffix_sha256='a'*64, cursor=1, last=4)
        first['node_resume'] = dict(sdk='7.47.0', expected=deepcopy(first['checkpoint']), delivered=3,
                                    aggregate=True, past_terminal_events=0, invalid_cursor_status=400, foreign_status=404)
        second = deepcopy(first)
        second.update(restored=deepcopy(first['saved']),
                      saved=dict(response='resp_second', previous=parent, sha256='5'*64, peer=404))
        second['stream']['response'] = 'resp_second'
        second['checkpoint'].update(response='resp_second', resource_sha256='5'*64)
        with patch.object(layers.package, 'read', return_value={'sdk': sdk}):
            layers.check(second, first)
            for path, wrong in [(('colors', 'last_bottom_right'), 'yellow'),
                                (('colors', 'first_bottom_left'), 'blue'),
                                (('restored', 'response'), ids['inline']),
                                (('saved', 'previous'), ids['replay']),
                                (('history', 'inline', 'sha256'), hashes['uploaded']),
                                (('restored', 'peer'), 200), (('stream', 'deltas'), 0),
                                (('stream', 'response'), 'resp_first'),
                                (('checkpoint', 'cursor'), 2),
                                (('node_resume', 'expected', 'response'), 'resp_second'),
                                (('node_resume', 'expected', 'suffix_sha256'), 'b'*64),
                                (('node_resume', 'past_terminal_events'), 1),
                                (('node_resume', 'foreign_status'), 200)]:
                changed = deepcopy(second)
                obj = changed
                for key in path[:-1]:
                    obj = obj[key]
                obj[path[-1]] = wrong
                with self.subTest(path=path), self.assertRaises(RuntimeError):
                    layers.check(changed, first)


if __name__ == '__main__':
    unittest.main()
