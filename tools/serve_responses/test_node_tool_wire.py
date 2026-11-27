"""Counterexamples for the stock clients' five project-release exchanges."""
import copy
import json
from pathlib import Path
import unittest

from tools.serve_responses.node_tool_wire import check
from tools.serve_responses.sdk_capture import wire_events


class NodeToolWireTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2]
        cls.record = json.loads((root/'docs/evidence/serve-stream-media-20261127/tool-wire.json').read_bytes())

    def test_real_events_and_payloads(self):
        r = self.record
        full, resumed, _ = check(r['exchanges'], r['sdk'], r['release_revision'])
        self.assertEqual(len(full), r['sdk']['frames'])
        self.assertEqual(len(resumed), r['sdk']['resumed'])

    def test_invalid_cursor_and_truncated_events(self):
        r = self.record
        suffix = r['exchanges']['current']['body']
        for after in [True, -2, 0, r['sdk']['cursor'] + 1]:
            with self.subTest(after=after), self.assertRaises(RuntimeError):
                wire_events(suffix, after=after)
        with self.assertRaises(RuntimeError):
            wire_events(suffix.rstrip(), after=r['sdk']['cursor'])

    def test_mismatched_exchanges_fail(self):
        r = self.record
        def change_event(packet, position, update):
            rows = wire_events(packet['body'], after=r['sdk']['cursor'])
            update(rows[position])
            packet['body'] = ''.join('event: '+e['type']+'\r\ndata: '+json.dumps(e)+'\r\n\r\n' for e in rows)

        changes = [
            ('current', lambda p: change_event(p, 0, lambda e: e.update(name='get_project_release'))),
            ('current', lambda p: change_event(p, -1, lambda e: e['response']['usage'].update(output_tokens=9000))),
            ('retained', lambda p: change_event(p, 0, lambda e: e.update(item_id='fc_other'))),
            ('retained', lambda p: change_event(p, -1, lambda e: e['response'].update(id='resp_unrelated'))),
            ('foreign', lambda p: p.update(status=200)),
            ('foreign', lambda p: p.update(body=json.dumps({'error': {'code': 'permission_denied'}}))),
            ('current', lambda p: p.update(path=p['path'].replace('starting_after=', 'after='))),
            ('answer', lambda p: p['input']['input'][0].update(call_id='call_unrelated')),
            ('answer', lambda p: p['input']['input'][0].update(output='{"revision":1}')),
            ('answer', lambda p: p['input'].update(previous_response_id='resp_unrelated')),
            ('live', lambda p: p['input'].update(tool_choice='none')),
        ]
        for index, (name, change) in enumerate(changes):
            with self.subTest(case=index, exchange=name):
                packets = copy.deepcopy(r['exchanges'])
                change(packets[name])
                with self.assertRaises(RuntimeError):
                    check(packets, r['sdk'], r['release_revision'])
