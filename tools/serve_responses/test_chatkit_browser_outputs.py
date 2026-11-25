"""Regression checks for the wire declarations consumed by browser clients."""
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

DOCS = Path(__file__).resolve().parents[2] / 'docs'


class BrowserOutputTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = json.loads((DOCS / 'serve-openapi-bundle-v1.json').read_bytes())
        cls.operation = cls.spec['paths']['/v1/chatkit']['post']
        cls.media = cls.operation['responses']['200']['content']
        cls.results = cls.operation['x-q4t-result-by-request-type']
        cls.events = cls.operation['x-q4t-event-by-request-type']

    def valid(self, declaration, value):
        return Draft202012Validator(dict(declaration, components=self.spec['components']),
                                    format_checker=Draft202012Validator.FORMAT_CHECKER).is_valid(value)

    def test_all_requests_select_their_response_media(self):
        types = self.spec['components']['schemas']['serve-application-v1.schema.json']['$defs']
        names = types['chatkit_client_request']['properties']['type']['enum']
        self.assertEqual(set(names), self.results.keys() | self.events.keys())
        self.assertFalse(self.results.keys() & self.events.keys())
        self.assertEqual((len(self.results), len(self.events)), (10, 6))
        self.assertTrue(all(ref == self.media['text/event-stream']['x-q4t-event-schema']
                            for ref in self.events.values()))

    def test_upload_completion_accepts_file_or_image_with_preview(self):
        document = dict(id='file_a', type='file', name='note.txt', mime_type='text/plain')
        image = dict(id='file_b', type='image', name='picture.png', mime_type='image/png',
                     preview_url='data:image/png;base64,iVBORw0KGgo=')
        for path, verb in [('/v1/chatkit/attachments', 'post'),
                           ('/v1/chatkit/attachments/{session_id}/{file_id}', 'put')]:
            response = self.spec['paths'][path][verb]['responses']['200']['content']
            schema = response['application/json']['schema']
            self.assertTrue(self.valid(schema, document))
            self.assertTrue(self.valid(schema, image))
            self.assertFalse(self.valid(schema, dict(document, type='image')))
            self.assertFalse(self.valid(schema, dict(image, preview_url='https://elsewhere.test/')))
            self.assertFalse(self.valid(schema, dict(image, upload_descriptor={})))

    def test_json_bodies_are_selected_by_operation(self):
        empty_page = dict(data=[], has_more=False, after=None)
        for name in ('threads.list', 'items.list'):
            self.assertTrue(self.valid(self.results[name], empty_page))
            self.assertFalse(self.valid(self.results[name], dict(empty_page, data=[42])))
            self.assertFalse(self.valid(self.results[name], {}))
        sync = self.results['threads.sync_custom_action']
        self.assertTrue(self.valid(sync, {'updated_item': None}))
        self.assertFalse(self.valid(sync, {'updated_item': 33}))
        self.assertFalse(self.valid(sync, {}))
        self.assertFalse(self.valid(self.results['threads.get_by_id'],
                                    {'id': 1, 'created_at':'not-a-date'}))
        for name in ('threads.delete', 'items.feedback', 'attachments.delete'):
            self.assertTrue(self.valid(self.results[name], {}))
            self.assertFalse(self.valid(self.results[name], empty_page))

    def test_actions_require_tagged_events(self):
        event = self.events['threads.custom_action']
        good = [dict(type='stream_options', stream_options=dict(allow_cancel=True)),
                dict(type='notice', level='info', message='Saved'),
                dict(type='thread.item.removed', item_id='item_widget')]
        for value in good:
            self.assertTrue(self.valid(event, value), value)
        bad = [{}, {'item':None}, {'text':'missing tag'}, dict(type='thread.item.removed'),
               dict(type='notice', level='unknown', message='Saved'),
               dict(type='stream_options', stream_options=dict(allow_cancel=None))]
        for value in bad:
            self.assertFalse(self.valid(event, value), value)
