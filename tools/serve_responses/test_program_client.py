"""Check that strict program events preserve the caller and reject malformed items."""
from importlib.util import find_spec
import unittest


@unittest.skipUnless(find_spec("openai"), "requires the OpenAI Python SDK")
class ProgramEvents(unittest.TestCase):
    @staticmethod
    def event(item):
        from pydantic import TypeAdapter
        from tools.serve_responses.program_client import ProgramStreamEvent

        data = {"type": "response.output_item.done", "output_index": 0,
                "sequence_number": 7, "item": item}
        return TypeAdapter(ProgramStreamEvent).validate_python(data, strict=True)

    def test_program_and_result_round_trip(self):
        from pydantic import ValidationError

        for item, required in [
            (dict(type="program", id="pr_1", call_id="call_1", code="return 1",
                  fingerprint="opaque"), "fingerprint"),
            (dict(type="program_output", id="po_1", call_id="call_1", result="1",
                  status="completed"), "result"),
        ]:
            with self.subTest(kind=item["type"]):
                self.assertEqual(self.event(item).item.to_dict(), item)
                with self.assertRaises(ValidationError):
                    self.event({k: v for k, v in item.items() if k != required})

    def test_child_call_requires_its_program_id(self):
        from pydantic import ValidationError

        item = dict(type="function_call", name="read_revision", call_id="call_2",
                    id="fc_1", arguments="{}", caller=dict(type="program", caller_id="call_1"))
        self.assertEqual(self.event(item).item.caller.caller_id, "call_1")
        with self.assertRaises(ValidationError):
            self.event(dict(item, caller={"type": "program"}))
