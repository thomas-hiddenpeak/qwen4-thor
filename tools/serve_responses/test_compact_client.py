"""Exercise stored compact resources using the installed SDK's strict types."""
from importlib.util import find_spec
import unittest


@unittest.skipUnless(find_spec("openai"), "requires the OpenAI Python SDK")
class CompactReader(unittest.TestCase):
    @staticmethod
    def window(output):
        return dict(id="cmp_1", object="response.compaction", created_at=1, output=output,
                    usage=dict(input_tokens=2, output_tokens=1, total_tokens=3,
                               input_tokens_details={"cached_tokens": 0, "cache_write_tokens": 0},
                               output_tokens_details={"reasoning_tokens": 0}))

    def test_saved_error_representations_preserve_their_wire_type(self):
        from pydantic import ValidationError
        from tools.serve_responses.compact_client import CompactWindow

        item = dict(type="mcp_call", id="mcp_1", arguments="{}", name="read",
                    server_label="project")
        for error in ('{"code":"historical"}',
                      dict(type="http_error", code=503, message="offline"),
                      dict(type="mcp_protocol_error", code=-1, message="closed"),
                      dict(type="mcp_tool_execution_error", content={"code": "denied"})):
            raw = self.window([dict(item, error=error)])
            with self.subTest(error=error):
                self.assertEqual(CompactWindow.model_validate(raw, strict=True).to_dict(), raw)
        for error in ({"type": "unknown"}, {"type": "http_error", "code": True},
                      {"type": "mcp_tool_execution_error"}):
            with self.subTest(error=error), self.assertRaises(ValidationError):
                CompactWindow.model_validate(self.window([dict(item, error=error)]),
                                             strict=True)

    def test_sealed_windows_require_the_memory_and_approval_fields(self):
        from pydantic import ValidationError
        from tools.serve_responses.compact_client import CompactWindow

        raw = self.window([dict(id="c_1", type="compaction", encrypted_content="sealed"),
                           dict(id="a_1", type="mcp_approval_response", approve=False,
                                approval_request_id="req_1")])
        self.assertEqual(CompactWindow.model_validate(raw, strict=True).to_dict(), raw)
        for index, field in [(0, "encrypted_content"), (1, "approve"), (1, "approval_request_id")]:
            invalid = [item.copy() for item in raw["output"]]
            del invalid[index][field]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                CompactWindow.model_validate(dict(raw, output=invalid), strict=True)
