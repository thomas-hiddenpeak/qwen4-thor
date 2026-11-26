"""Check actual stored native text, still images and video via the OpenAI SDK."""
import base64
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import ssl
import sys
from urllib.parse import urlsplit

import openai


def check(config):
    destination = Path(config["capture"])
    destination.mkdir(mode=0o700)
    address = urlsplit(config["base"])
    assert address.hostname == "127.0.0.1" and address.scheme == "https"
    tls = ssl.create_default_context(cafile=config["certificate"])
    with ExitStack() as clients:
        def client(key):
            return clients.enter_context(openai.OpenAI(
                base_url=config["base"], api_key=key, max_retries=0, timeout=75,
                http_client=openai.DefaultHttpxClient(verify=tls)))
        owner, peer = client(config["key"]), client(config["other"])
        green, blue = [base64.b64encode(Path(path).read_bytes()).decode()
                       for path in config["colors"]]
        uri = "data:image/png;base64,"
        parts = [{"text": "Inspect the still image and the video. "},
                 {"image_url": green},
                 {"type": "video_frames", "video_frames": [uri+blue, {"url": uri+blue}],
                  "video_timestamps": [0, 1]},
                 {"type": "text", "text": "What color is the still image? Reply with one English color word."}]
        args = dict(model="qwen3.8-flash-next", store=True, temperature=0,
                    reasoning_effort="none", max_completion_tokens=32)
        messages = [dict(role="user", content="I will send an image and a clip."),
                    dict(role="assistant", content=[{"text": "Ready"}, {"type": "text", "text": "."}]),
                    dict(role="user", content=parts)]
        completion = owner.chat.completions.create(messages=messages, **args)
        reply = completion.choices[0].message.content
        assert completion.choices[0].finish_reason == "stop" and reply, reply
        get = owner.chat.completions.messages
        response = get.with_raw_response.list(completion.id, order="asc").http_response
        response.raise_for_status()
        (destination/"history.json").write_bytes(response.content)
        page = response.json()
        first, assistant, visual = page["data"]
        assert [m["role"] for m in page["data"]] == ["user", "assistant", "user"]
        assert page["first_id"] == first["id"] and page["last_id"] == visual["id"]
        assert assistant["content"] == "Ready." and assistant["original_content_parts"] == messages[1]["content"]
        assert visual["content"] == parts[0]["text"] + parts[-1]["text"]
        assert visual["original_content_parts"] == parts
        projected = visual["content_parts"]
        assert [p["type"] for p in projected] == ["text", "image_url", "image_url", "image_url", "text"]
        assert [p["image_url"]["url"] for p in projected[1:4]] == [uri+green, uri+blue, uri+blue]
        parsed = get.list(completion.id, order="desc", after=visual["id"], limit=1)
        assert parsed.has_more and [(m.id, m.content) for m in parsed.data] == [(assistant["id"], "Ready.")]
        try:
            peer.chat.completions.messages.list(completion.id)
        except openai.NotFoundError as error:
            assert error.status_code == 404
        else:
            raise AssertionError("peer read stored material")
        plain = owner.chat.completions.create(model=args["model"], temperature=0,
            reasoning_effort="none", max_completion_tokens=24,
            messages=[dict(role="user", content="Return only the integer 720041.")])
        independent = plain.choices[0].message.content.strip()
        assert independent == "720041" and plain.id != completion.id
        result = dict(sdk=openai.__version__, chat_id=completion.id, reply=reply,
            color_correct=reply.strip().casefold().rstrip(".") == "green",
            independent_chat=independent, message_ids=[m["id"] for m in page["data"]],
            peer=404, history_sha256=hashlib.sha256(response.content).hexdigest(),
            frame_count=2, cursor=assistant["id"])
        return result


if __name__ == "__main__":
    print(json.dumps(check(json.load(sys.stdin)), ensure_ascii=False))
