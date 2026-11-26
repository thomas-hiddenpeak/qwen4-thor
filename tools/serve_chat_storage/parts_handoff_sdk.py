"""Read the original stored visual page and exercise the successor with the SDK."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import ssl
import sys
from urllib.parse import urlsplit

import openai


def check_page(owner, peer, chat_id, reference, capture):
    messages = owner.chat.completions.messages
    raw = messages.with_raw_response.list(chat_id, order="asc").http_response
    raw.raise_for_status()
    page = raw.json()
    rows = page["data"]
    ids = [row["id"] for row in rows]
    assert page["object"] == "list" and page["has_more"] is False
    assert len(ids) == len(set(ids)) == 3 and (page["first_id"], page["last_id"]) == (ids[0], ids[-1])
    for observed, accepted in zip(rows, reference["data"], strict=True):
        assert dict(observed, id=accepted["id"]) == accepted
    cursor = messages.list(chat_id, order="desc", after=ids[-1], limit=1)
    assert cursor.has_more and [(m.id, m.content) for m in cursor.data] == [(ids[1], "Ready.")]
    try:
        peer.chat.completions.messages.list(chat_id)
    except openai.NotFoundError as error:
        assert error.status_code == 404
    else:
        raise AssertionError("a second client read the stored history")
    capture.write_bytes(raw.content)
    return dict(chat_id=chat_id, message_ids=ids, cursor=ids[1], peer=404,
                history_sha256=hashlib.sha256(raw.content).hexdigest())


def run(config):
    capture = Path(config["capture"])
    capture.mkdir(mode=0o700)
    target = urlsplit(config["base"])
    assert target.hostname == "127.0.0.1"
    if target.scheme == "https":
        verify = ssl.create_default_context(cafile=config["certificate"])
    else:
        assert target.scheme == "http" and config["handoff"] and not config["certificate"]
        verify = True
    reference = json.loads(Path(config["proof"]).read_bytes())["wire_history"]
    with ExitStack() as resources:
        def client(key):
            return resources.enter_context(openai.OpenAI(
                base_url=config["base"], api_key=key, max_retries=0, timeout=75,
                http_client=openai.DefaultHttpxClient(verify=verify)))
        owner, peer = client(config["key"]), client(config["other"])
        restored = None
        if previous := config.get("previous"):
            restored = check_page(owner, peer, previous["chat_id"], reference,
                                  capture/"restored-history.json")
            assert restored == previous

        messages = [dict(role=row["role"], content=row.get("original_content_parts", row["content"]))
                    for row in reference["data"]]
        options = dict(model="qwen3.8-flash-next", temperature=0, reasoning_effort="none",
                       max_completion_tokens=32)
        response = owner.chat.completions.create(messages=messages, store=True, **options)
        reply = response.choices[0].message.content.strip()
        saved = check_page(owner, peer, response.id, reference, capture/"current-history.json")
        core = owner.chat.completions.create(messages=[
            dict(role="user", content="Return only the integer 720041.")], **options)
        (capture/"completion.json").write_text(response.model_dump_json(exclude_unset=True))
        assert core.id != response.id and core.choices[0].finish_reason == "stop"
        return dict(sdk=openai.__version__, saved=saved, restored=restored, reply=reply,
                    finish=response.choices[0].finish_reason,
                    independent_chat=core.choices[0].message.content.strip())


if __name__ == "__main__":
    print(json.dumps(run(json.load(sys.stdin)), ensure_ascii=False))
