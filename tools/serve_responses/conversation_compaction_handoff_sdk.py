"""Read the prior sealed conversation and use its ciphertext in a fresh model call."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys

import openai


def exercise(params):
    out = Path(params["capture"])
    out.mkdir(mode=0o700)
    captures = {}

    def record(name, reply):
        wire = reply.http_response
        wire.raise_for_status()
        data = wire.content
        (out / (name + ".json")).write_bytes(data)
        captures[name] = hashlib.sha256(data).hexdigest()
        return wire.json()

    def sealed(page):
        rows = page["data"]
        assert (len(rows) == 3 and [item["type"] for item in rows] ==
                ["message", "message", "compaction"])
        item = rows[-1]
        assert item["encrypted_content"].startswith("q4tc1:" + item["id"] + ":")
        return item

    with ExitStack() as stack:
        def connect(key):
            verify = params.get("certificate") or True
            return stack.enter_context(openai.OpenAI(
                base_url=params["base"], api_key=key, max_retries=0, timeout=90,
                http_client=openai.DefaultHttpxClient(verify=verify)))
        client, peer = connect(params["key"]), connect(params["other"])

        def check_owner(conversation):
            try:
                peer.conversations.items.list(conversation)
            except openai.NotFoundError as error:
                assert error.status_code == 404
                return 404
            raise AssertionError("A different client read the conversation")

        def observe(conversation, label):
            page = record(label+"_page", client.conversations.items.with_raw_response.list(
                conversation, order="asc"))
            item = sealed(page)
            individual = record(label+"_seal", client.conversations.items.with_raw_response.retrieve(
                item["id"], conversation_id=conversation))
            assert individual == item
            summary = dict(conversation=conversation, items=[v["id"] for v in page["data"]],
                           seal=item["id"], page_sha256=captures[label+"_page"],
                           seal_sha256=captures[label+"_seal"], peer=check_owner(conversation))
            # This request contains only the sealed bytes and the question.
            # A successful response depends on the restored service's state key.
            answered = client.responses.with_raw_response.create(
                model="qwen3.8-flash-next", store=False, temperature=0, max_output_tokens=128,
                input=[individual, {"role":"user","content":"Return only the project handoff number."}])
            record(label+"_answer", answered)
            return summary, answered.parse().output_text.strip()

        restored, recovered_answer = None, None
        if previous := params.get("previous"):
            restored, recovered_answer = observe(previous["conversation"], "restored")
            assert restored == previous
        new = record("create", client.conversations.with_raw_response.create(items=[
            {"role":"user","content":"Remember the project register entry."},
            {"role":"assistant","content":"The current project handoff number is 28617."
             + " This copy repeats the same register entry." * 36},
            {"type":"compaction_trigger"}]))
        saved, answer = observe(new["id"], "current")
        simple = client.chat.completions.with_raw_response.create(
            model="qwen3.8-flash-next", max_completion_tokens=64, temperature=0,
            messages=[{"role":"user","content":"Reply with exactly 720041 and nothing else."}])
        record("independent", simple)
        control = simple.parse().choices[0].message.content.strip()
    return dict(sdk=openai.__version__, saved=saved, restored=restored,
                recovered_answer=recovered_answer, answer=answer, independent_chat=control,
                wire_sha256=captures)


if __name__ == "__main__":
    print(json.dumps(exercise(json.load(sys.stdin))))
