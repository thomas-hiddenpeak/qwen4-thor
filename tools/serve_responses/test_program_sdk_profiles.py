"""Check stored tool identity and cursor recovery on the two SDK wires."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tools.serve_responses import program_sdk_profiles as profile
from tools.serve_service import package


class StoredFunction(unittest.TestCase):
    def setUp(self):
        base = package.ROOT/'build/program-sdk-profile-tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.call = dict(type='function_call', id='fc_1', call_id='call_1',
                         name='get_project_release', arguments='{}')
        self.marker = dict(type='response.function_call_arguments.done', sequence_number=1,
                           item_id='fc_1', output_index=1, name='get_project_release', arguments='{}')
        self.origin = dict(response_id='resp_initial', item_id='fc_1', call_id='call_1',
                           function='get_project_release', marker=self.marker, frames=3, cursor=0,
                           followup='resp_saved', revision=566)
        self.body = dict(id='resp_initial', status='completed', error=None,
                         incomplete_details=None, output=[{'type': 'reasoning'}, self.call])
        self.answer = dict(id='resp_saved', previous_response_id='resp_initial', status='completed',
                           error=None, incomplete_details=None, output=[dict(type='message', status='completed',
                           content=[dict(type='output_text', text='566')])])

    def capture(self, version):
        marker = self.marker.copy()
        if version == profile.VERSIONS[1]:
            del marker['name']
        events = [dict(type='response.created', sequence_number=0), marker,
                  dict(type='response.completed', sequence_number=2, response=self.body)]
        rows = dict(call=self.body, answer=self.answer, events=events, resumed=events[1:],
                    continuation=dict(self.answer, id='resp_new'))
        for name, value in rows.items():
            package.save(self.folder/(name+'.json'), value)
        return rows

    def inspect(self, version):
        return profile.inspect_capture(self.folder, version, self.origin, 566, True)

    def test_legacy_and_current_recovered_call(self):
        for version in profile.VERSIONS:
            self.capture(version)
            self.assertEqual(self.inspect(version)['continued'], 'resp_new')

    def test_bad_streams_or_continuations_cannot_qualify(self):
        for version in profile.VERSIONS:
            for name, change in [
                    ('events', lambda v: v[1].update(item_id='fc_other')),
                    ('events', lambda v: v[1].update(output_index=0)),
                    ('events', lambda v: v[-1].update(sequence_number=1)),
                    ('resumed', lambda v: v.pop(0)),
                    ('resumed', lambda v: v[0].update(name='some_other_function')),
                    ('call', lambda v: v['output'][1].update(call_id='call_other')),
                    ('call', lambda v: v.update(status='incomplete')),
                    ('answer', lambda v: v.update(previous_response_id='resp_other')),
                    ('answer', lambda v: v['output'][0].update(status='incomplete')),
                    ('continuation', lambda v: v.update(status='incomplete')),
                    ('continuation', lambda v: v.update(id='resp_saved')),
                    ('continuation', lambda v: v['output'][0]['content'][0].update(text='wrong'))]:
                with self.subTest(version=version, name=name):
                    value = deepcopy(self.capture(version)[name])
                    change(value)
                    package.save(self.folder/(name+'.json'), value)
                    with self.assertRaises(RuntimeError):
                        self.inspect(version)
