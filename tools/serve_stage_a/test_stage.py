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

    def test_next_request_waits_for_owner_cleanup_after_slot_release(self):
        run = object.__new__(v.Run)
        from unittest.mock import Mock
        run.observe = Mock()
        with patch.object(v.r.Run, "wait_slot") as slot:
            run.wait_slot("next")
            slot.assert_called_once_with("next")
        predicate = run.observe.call_args.args[0]
        self.assertFalse(predicate(dict(q4t_active_chats=1, q4t_structured_active=1,
                                        q4t_structured_workers_active=1)))
        self.assertTrue(predicate(dict(q4t_active_chats=0, q4t_structured_active=0,
                                       q4t_structured_workers_active=0)))

    def test_finish_keeps_only_remaining_budget(self):
        import finish
        run = object.__new__(finish.FinishRun)
        run.installed, run.deadline = False, time.time() + 60
        run.requests, run.negative, run.starts, run.capability_gets = 6, 1, 1, 1
        run.budget()
        run.starts = 2
        with self.assertRaises(AssertionError):
            run.budget()

    def test_slow_probe_reaches_incremental_writer_and_last_start_is_bounded(self):
        body = v.slow_reader_request()
        self.assertTrue(body["stream"])
        self.assertNotIn("response_format", body)
        self.assertEqual(body["request_id"], "transport-slow")
        self.assertEqual(len(body["cancel_token"]), 64)
        last = stage.module("last_contract", Path(__file__).with_name("closeout.py"))
        run = object.__new__(last.Remaining)
        run.deadline = time.time() + 60
        run.requests, run.negative, run.starts, run.capability_gets = 4, 0, 1, 1
        run.budget()
        run.requests = 5
        with self.assertRaises(RuntimeError):
            run.budget()


if __name__ == "__main__":
    unittest.main()
