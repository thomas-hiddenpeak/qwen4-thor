"""Offline mutation checks for transport evidence and guarded delivery."""
import copy
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import stage
v = stage.module("transport_test", Path(__file__).with_name("verify.py"))


def chunk():
    return dict(id="c", model="m", created=1, choices=[dict(index=0, delta={},
                logprobs=None, finish_reason=None)], usage=None)


class Contracts(unittest.TestCase):
    def test_nullable_wire_not_sdk_defaults(self):
        value = chunk()
        v.nullable_contract([value], True)
        for field in ("logprobs", "usage"):
            broken = copy.deepcopy(value)
            del (broken["choices"][0] if field == "logprobs" else broken)[field]
            with self.assertRaises(AssertionError):
                v.nullable_contract([broken], True)

    def test_identity_cannot_change_in_stream(self):
        first, second = chunk(), chunk()
        second["id"] = "different"
        with self.assertRaises(AssertionError):
            v.nullable_contract([first, second], True)

    def test_done_and_error_order(self):
        raw = b': keep-alive\r\n\r\ndata: ' + json.dumps(chunk()).encode() + b'\r\n\r\ndata: [DONE]\r\n\r\n'
        self.assertEqual(len(v.events(raw)), 1)
        for bad in (raw + b'data: [DONE]\n\n', raw + b'data: {}\n\n', raw.replace(b'[DONE]', b'{}')):
            with self.assertRaises(AssertionError):
                v.events(bad)

    def test_error_location_is_not_lost(self):
        value = dict(error=dict(message="bad", type="invalid_request_error", code="invalid_request", param="temperature"))
        v.error_contract(json.dumps(value), "invalid_request", "temperature")
        value["error"]["param"] = None
        with self.assertRaises(AssertionError):
            v.error_contract(json.dumps(value), "invalid_request", "temperature")

    def test_stage_budget_cannot_expand(self):
        run = object.__new__(v.Run)
        run.installed = False
        run.deadline = time.time() + 60
        run.requests, run.negative, run.starts, run.capability_gets = 10, 16, 2, 2
        run.budget()
        with self.assertRaises(AssertionError):
            run.counted()
        run.requests = 10
        run.deadline = time.time() - 1
        with self.assertRaises(AssertionError):
            run.budget()

    def test_installation_requires_guard_before_state_read(self):
        stage.bind()
        with patch.object(stage.h, "require_guard", side_effect=RuntimeError("no lease")), \
             patch.object(stage.a, "check") as checked:
            with self.assertRaises(RuntimeError):
                stage.a.execute()
            checked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
