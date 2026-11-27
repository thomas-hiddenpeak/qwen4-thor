"""Check the SDK acceptance of a completed video continuation."""
from importlib.util import find_spec
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


@unittest.skipUnless(find_spec("openai"), "requires the OpenAI Python SDK")
class VideoContinuation(unittest.TestCase):
    def test_partial_or_failed_reply_cannot_pass_by_matching_the_color(self):
        from tools.serve_responses.compact_window_sdk import verify_video

        source = {"id": "msg_clip", "type": "message", "role": "user",
                  "status": "completed", "content": [
                      {"type": "input_file", "filename": "clip.mp4", "file_id": "file_clip"}]}
        record = dict(message=source, question="Which color?", report="Prior review.", answer="Blue")
        window = {
            "id": "resp_compact", "object": "response.compaction", "created_at": 1,
            "output": [source, {"type": "compaction", "id": "cmp_1", "encrypted_content": "opaque"}],
            "usage": {"input_tokens": 4, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                      "output_tokens": 1, "output_tokens_details": {"reasoning_tokens": 0},
                      "total_tokens": 5},
        }
        api = Mock()
        def capture(name, raw):
            return window if name == "video_compact" else {}
        for status, error, details, message_status, text, accepted in [
            ("completed", None, None, "completed", "Blue", True),
            ("completed", None, None, "completed", "Red", False),
            ("incomplete", None, {"reason": "max_output_tokens"}, "incomplete", "Blue", False),
            ("completed", None, None, "incomplete", "Blue", False),
            ("completed", {"code": "server_error"}, None, "completed", "Blue", False),
            ("completed", None, {"reason": "max_output_tokens"}, "completed", "Blue", False),
        ]:
            with self.subTest(status=status, text=text, error=error, details=details, message_status=message_status):
                reply = SimpleNamespace(status=status, error=error, incomplete_details=details,
                                        output_text=text, output=[SimpleNamespace(type="message", status=message_status)])
                api.responses.with_raw_response.create.return_value.parse.return_value = reply
                if status != "completed" or message_status != "completed" or error or details:
                    with self.assertRaisesRegex(ValueError, "Video continuation did not complete"):
                        verify_video(api, "model", record, capture)
                else:
                    self.assertEqual(verify_video(api, "model", record, capture)["exact_answer"], accepted)
