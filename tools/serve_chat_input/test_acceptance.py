"""Driver negative controls; no listeners, model, GPU or SDK HTTP calls."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_chat_input.acceptance import LIMITS, requests, require_answer
from tools.serve_protocol.check_contract import load_validators


class Acceptance(unittest.TestCase):
    def test_real_answer_required(self):
        def value(text="42", reason="stop", **extra):
            return SimpleNamespace(finish_reason=reason, message=dict(content=text, **extra))
        require_answer(value(), "42")
        for actual in (value(""), value("271828"), value(reason="length"),
                       value(tool_calls=[dict(id="not-an-answer")])):
            with self.assertRaises(RuntimeError):
                require_answer(actual, "42")

    def test_budget_and_business_shapes(self):
        self.assertEqual(LIMITS, dict(starts=2, chats=8, controls=2000, seconds=900))
        bodies = requests()
        self.assertEqual(len(bodies)+1, 6)  # Generated tool-result follow-up.
        schema = load_validators()["chat"]
        for body in bodies.values():
            schema.validate(body)
        self.assertEqual(bodies["named"]["messages"][1]["name"], "Alice")
        self.assertEqual(bodies["named"]["messages"][2]["name"], "Bob")
        self.assertNotEqual(bodies["named"]["messages"][1]["content"],
                            bodies["named"]["messages"][2]["content"])
        self.assertEqual(bodies["no_user"]["messages"][0]["role"], "developer")

    def test_frozen_requests_reach_native_plan(self):
        probe = Path(os.environ["Q4T_CHAT_INPUT_PROBE"])
        bodies = list(requests().values())
        malformed = copy.deepcopy(bodies[0])
        malformed["messages"][1]["content"] = None
        run = subprocess.run([str(probe)], input="".join(json.dumps(dict(raw=json.dumps(b)))+"\n"
            for b in [*bodies, malformed]), text=True, capture_output=True, timeout=15)
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.stderr, "")
        actual = list(map(json.loads, run.stdout.splitlines()))
        self.assertEqual([r["accepted"] for r in actual], [True]*len(bodies)+[False])
        self.assertEqual(actual[-1]["response"]["error"]["param"], "messages[1].content")


if __name__ == "__main__":
    unittest.main()
