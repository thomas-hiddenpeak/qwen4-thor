"""Exercise native Conversations compaction and resume through an official SDK."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys

import openai


def run(params):
    destination = Path(params["capture"])
    destination.mkdir(mode=0o700)
    files = {}

    def capture(label, result):
        wire = result.http_response
        wire.raise_for_status()
        path = destination / (label + ".json")
        path.write_bytes(wire.content)
        files[label] = hashlib.sha256(wire.content).hexdigest()
        return wire.json()

    def history(fact):
        return [
            {"role": "user", "content": "Please remember the project register entry."},
            {"role": "assistant", "content": fact + " This copy repeats the same register entry." * 36},
            {"type": "compaction_trigger"},
        ]

    def sealed(item):
        assert item["type"] == "compaction" and item["id"].startswith("cmp_")
        assert item["encrypted_content"].startswith("q4tc1:" + item["id"] + ":")
        return item["id"]

    with ExitStack() as stack:
        def connect(key):
            return stack.enter_context(openai.OpenAI(
                base_url=params["base"], api_key=key, max_retries=0, timeout=90,
                http_client=openai.DefaultHttpxClient(verify=params["certificate"])))
        owner, peer = connect(params["key"]), connect(params["other"])
        service = owner.conversations
        metadata = {"purpose": "handoff"}
        first = capture("created", service.with_raw_response.create(
            items=history("The registered dispatch code is 61432."), metadata=metadata))
        ident = first["id"]
        assert first["object"] == "conversation" and first["metadata"] == metadata
        page = capture("original", service.items.with_raw_response.list(ident, order="asc"))
        assert len(page["data"]) == 3
        before = [row["id"] for row in page["data"]]
        first_seal = sealed(page["data"][-1])
        try:
            service.items.create(ident, items=[{"type": "compaction_trigger"},
                                               {"role": "user", "content": "late"}])
        except openai.BadRequestError as error:
            assert error.status_code == 400
        else:
            raise AssertionError("non-final trigger accepted")
        unchanged = capture("after_invalid", service.items.with_raw_response.list(ident, order="asc"))
        assert unchanged == page
        additional = capture("appended", service.items.with_raw_response.create(
            ident, items=history("The current project handoff number is 28617.")))
        assert len(additional["data"]) == 3
        second_seal = sealed(additional["data"][-1])
        assert first_seal != second_seal
        whole = capture("whole", service.items.with_raw_response.list(ident, order="asc"))
        assert whole["data"] == page["data"] + additional["data"]
        assert len({row["id"] for row in whole["data"]}) == 6
        for i, item_id in enumerate((first_seal, second_seal)):
            old = capture("sealed_" + str(i), service.items.with_raw_response.retrieve(
                item_id, conversation_id=ident))
            assert old == whole["data"][2 + 3*i]
        try:
            peer.conversations.items.list(ident)
        except openai.NotFoundError as error:
            assert error.status_code == 404
        else:
            raise AssertionError("peer obtained private history")
        resumed = owner.responses.with_raw_response.create(
            model="qwen3.8-flash-next", conversation=ident,
            input="Return only the current project handoff number.",
            max_output_tokens=128, temperature=0)
        capture("resumed", resumed)
        answer = resumed.parse().output_text.strip()
        chat = owner.chat.completions.with_raw_response.create(
            model="qwen3.8-flash-next", messages=[
                {"role":"user", "content":"Reply with exactly 720041 and nothing else."}],
            max_completion_tokens=64, temperature=0)
        capture("independent", chat)
        control = chat.parse().choices[0].message.content.strip()
    return dict(sdk=openai.__version__, conversation=ident, original_ids=before,
                compaction_ids=[first_seal, second_seal], append_items=3,
                peer=404, invalid_trigger=400, answer=answer, independent_chat=control,
                exact_answer=(answer == "28617"), files=files)


if __name__ == "__main__":
    print(json.dumps(run(json.load(sys.stdin))))
