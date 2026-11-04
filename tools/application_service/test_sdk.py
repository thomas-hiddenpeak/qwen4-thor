"""Exercise the exact pinned SDK adapter without sockets or model execution."""
import json
from pathlib import Path
import tempfile
import unittest

import httpx2
import openai
import verify


class SdkContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = verify.Run(dict(model_dir="unused", protocol_sha256="a" * 64,
            sdk_version="3.42.1"), Path(self.temp.name) / "checks")
        self.run.wait_slot = lambda _name: None
        self.folder = self.run.out

    def client(self, body, status=200, sse=False):
        def respond(request):
            return httpx2.Response(status, content=body,
                headers={"content-type": "text/event-stream" if sse else
                                          "application/json"}, request=request)
        client = openai.OpenAI(api_key="local-test", base_url="http://localhost/v1",
            max_retries=0, _strict_response_validation=True,
            http_client=openai.DefaultHttpxClient(
                transport=httpx2.MockTransport(respond), trust_env=False))
        self.addCleanup(client.close)
        return client

    def test_json_sdk_typed_parse_and_wire(self):
        body = dict(id="id", model="qwen3.8-flash-next", created=1700000000,
            object="chat.completion", choices=[dict(index=0, finish_reason="stop",
                message=dict(role="assistant", content="710003"))],
            usage=dict(prompt_tokens=10, completion_tokens=2, total_tokens=12))
        result = self.run.sdk_chat(self.client(json.dumps(body)), self.folder,
                                  "json", self.run.request_body())
        self.assertEqual(result.message["content"], "710003")

    def test_sse_capture_does_not_change_sdk_stream(self):
        events = []
        for delta, finish in [({"role": "assistant", "content": ""}, None),
                              ({"content": "710003"}, None), ({}, "stop")]:
            events.append(dict(id="id", model="qwen3.8-flash-next", created=42,
                object="chat.completion.chunk", choices=[dict(index=0,
                    delta=delta, finish_reason=finish)]))
        events.append(dict(id="id", model="qwen3.8-flash-next", created=42,
            object="chat.completion.chunk", choices=[], usage=dict(
                prompt_tokens=10, completion_tokens=2, total_tokens=12)))
        raw = "".join("data: " + json.dumps(e) + "\r\n\r\n" for e in events)
        raw += "data: [DONE]\r\n\r\n"
        result = self.run.sdk_chat(self.client(raw, sse=True), self.folder,
                                  "sse", self.run.request_body(True))
        self.assertEqual(result.message["content"], "710003")
        self.assertEqual((self.folder / "sse-wire.bin").read_bytes(), raw.encode())

    def test_sdk_bad_request_class_and_fields(self):
        body = dict(error=dict(message="invalid budget", type="invalid_request_error",
                               code="invalid_request", param=None))
        self.run.sdk_chat(self.client(json.dumps(body), 400), self.folder,
            "bad", self.run.request_body(), error_status=400)
        result = json.loads((self.folder / "bad-error.json").read_text())
        self.assertEqual(result["sdk_class"], "BadRequestError")
        self.assertEqual(self.run.negative, 1)


if __name__ == "__main__":
    unittest.main()
