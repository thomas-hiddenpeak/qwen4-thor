"""Failure-sensitive checks before the bounded real schema/cache acceptance."""
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest

from tools.serve_schema_v2.acceptance import (
    check_features, check_final, requests, restore_record)


class Driver(unittest.TestCase):
    def test_frozen_requests_use_actual_production_planner(self):
        rows = requests()
        self.assertEqual(len(rows), 6)  # Plus two actual-result follow-up turns.
        data = "".join(json.dumps(dict(raw=json.dumps(body), session_cache=True,
            prefix_cache=True)) + "\n" for body in rows.values())
        result = subprocess.run([os.environ['Q4T_SCHEMA_PROBE']], input=data,
            capture_output=True, text=True, check=True, timeout=15)
        answers = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(answers), len(rows))
        self.assertTrue(all(row['accepted'] for row in answers), answers)

    def test_generated_values_require_exact_integer_and_real_terminal(self):
        actual = dict(answer=42, exact=9007199254740993,
            recorded_at='2024-02-29T12:00:00Z', note=None,
            node=dict(label='leaf', children=[]))
        def value(row=actual, finish='stop'):
            return SimpleNamespace(finish_reason=finish,
                message=dict(content=json.dumps(row)))
        self.assertEqual(check_features(value()), actual)
        for broken in (value(dict(actual, exact=9007199254740992)),
                       value(dict(actual, exact='9007199254740993')),
                       value(dict(actual, answer=44)), value(finish='length')):
            with self.assertRaises(RuntimeError): check_features(broken)

    def test_cache_log_requires_unique_request_layout_and_no_error(self):
        log = ('[session-cache] request=schema-v2-warm event=restore prefix=1024 '
               'ms=1.2 error=none source=shared layout=target\n')
        self.assertEqual(restore_record(log, 'schema-v2-warm'),
            dict(prefix=1024, source='shared', layout='target'))
        for bad in (log + log, log.replace('layout=target', 'layout=mtp'),
                    log.replace('error=none', 'error=bad'),
                    log.replace('request=schema-v2-warm', 'request=other')):
            with self.assertRaises(RuntimeError):
                restore_record(bad, 'schema-v2-warm')

    def test_final_answer_uses_actual_title_and_matching_tool_citation(self):
        source = dict(text="1: # MODEL.md — actual title\n2: body",
                      citation="[docs/MODEL.md:L1-L16]")
        actual = dict(title="MODEL.md — actual title", citation=source['citation'],
                      requested_line_count=16, note=None)
        def value(row=actual, finish='stop'):
            return SimpleNamespace(finish_reason=finish,
                message=dict(content=json.dumps(row)))
        self.assertEqual(check_final(value(), dict(source=source)), actual)
        for bad in (dict(actual, title="Qwen3.8-Flash-Next invented title"),
                    dict(actual, citation="[docs/MODEL.md:L2-L16]"),
                    dict(actual, requested_line_count=8), dict(actual, note="read")):
            with self.assertRaises(RuntimeError):
                check_final(value(bad), dict(source=source))
        with self.assertRaises(RuntimeError):
            check_final(value(finish='length'), dict(source=source))


if __name__ == '__main__': unittest.main()
