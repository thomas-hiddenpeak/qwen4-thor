"""Verify a replayable standalone compaction of a real stored tool decision."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys

import openai

from tools.serve_responses.compact_client import CompactWindow


def check_window(window, expected_user, question, original_error, legacy_error=False):
    if window.get("object") != "response.compaction":
        raise ValueError("Expected a standalone compaction")
    output = window["output"]
    users = [item for item in output if item.get("role") == "user"]
    if [u["id"] for u in users] != [expected_user["id"], question["id"]]:
        raise ValueError("Original user message identities changed")
    if users != [expected_user, question] or any(u["status"] != "completed" for u in users):
        raise ValueError("Original user message or status changed")
    failed, = [i for i in output if i["type"] == "mcp_call"]
    error = failed["error"]
    if isinstance(error, str) != legacy_error:
        raise ValueError("Unexpected MCP error representation")
    if legacy_error:
        error = json.loads(error)
    if failed["status"] != "failed" or error != original_error:
        raise ValueError("Retained MCP decision changed")
    memory, = [i for i in output if i["type"] == "compaction"]
    if not memory["encrypted_content"].startswith("q4tc1:" + memory["id"] + ":"):
        raise ValueError("Missing sealed compaction state")
    if any(i.get("role") == "assistant" for i in output):
        raise ValueError("Expected the archived assistant report to be summarized")
    return [u["id"] for u in users]


def verify_video(owner, model, record, capture):
    original = record["message"]
    question = dict(role="user", content=record["question"])
    raw = owner.responses.with_raw_response.compact(
        model=model, input=[original, dict(role="assistant", content=record["report"]),
                            question], instructions="Summarize the review briefly.")
    window = capture("video_compact", raw)
    typed = CompactWindow.model_validate(window, strict=True)
    if typed.to_dict() != window or not window["usage"]["output_tokens"]:
        raise ValueError("Video compaction did not produce a typed model summary")
    retained, = [i for i in window["output"] if i.get("id") == original["id"]]
    if retained != original:
        raise ValueError("Stored video file descriptor was changed")
    resumed = owner.responses.with_raw_response.create(
        model=model, input=window["output"], max_output_tokens=96, temperature=0,
        reasoning={"effort": "none"}, store=False)
    capture("video_resumed", resumed)
    text = resumed.parse().output_text.strip()
    return dict(answer=text, exact_answer=text.casefold() == record["answer"].casefold(),
                original_item=original["id"], file_preserved=(retained == original),
                usage=window["usage"])


def verify_saved_window(params):
    """Submit a previously issued window through the public strict SDK."""
    window = CompactWindow.model_validate(params["window"], strict=True)
    items = window.to_dict()["output"]
    if items != params["window"]["output"]:
        raise ValueError("Typed window changed the stored input")
    payload = dict(model="qwen3.8-flash-next", input=items, max_output_tokens=96,
                   temperature=0, reasoning={"effort": "none"}, store=False)
    target = Path(params["capture"])
    target.mkdir(mode=0o700)
    with ExitStack() as clients:
        def connect(key):
            return clients.enter_context(openai.OpenAI(
                api_key=key, base_url=params["base"], max_retries=0, timeout=90,
                _strict_response_validation=True,
                http_client=openai.DefaultHttpxClient(verify=params["certificate"])))
        try:
            connect(params["other"]).responses.create(**payload)
        except openai.BadRequestError as error:
            if error.code != "invalid_encrypted_content":
                raise
        else:
            raise ValueError("Foreign client opened the stored window")
        raw = connect(params["key"]).responses.with_raw_response.create(**payload)
        path = target / "response.json"
        path.write_bytes(raw.http_response.content)
        row = raw.parse()
        if row.status != "completed" or any(x.type == "mcp_call" for x in row.output):
            raise ValueError("Saved history caused another MCP call or an incomplete answer")
        answer = row.output_text.strip()
        return dict(sdk=openai.__version__, answer=answer,
                    exact_answer=answer.casefold() == params["answer"].casefold(),
                    foreign_code="invalid_encrypted_content",
                    usage=row.usage.to_dict(), http_status=raw.http_response.status_code,
                    response_sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def run(params):
    target = Path(params["capture"])
    target.mkdir(mode=0o700)
    hashes = {}
    varies = {}

    def capture(label, raw):
        response = raw.http_response
        response.raise_for_status()
        varies[label] = sorted({h.strip().lower() for h in response.headers.get("vary", "").split(",") if h.strip()})
        path = target / (label + ".json")
        path.write_bytes(response.content)
        hashes[label] = hashlib.sha256(response.content).hexdigest()
        return response.json()

    with ExitStack() as clients:
        def connect(key):
            return clients.enter_context(openai.OpenAI(
                api_key=key, base_url=params["base"], max_retries=0, timeout=90,
                _strict_response_validation=True,
                http_client=openai.DefaultHttpxClient(verify=params["certificate"])))

        owner, peer = connect(params["key"]), connect(params["other"])
        model = "qwen3.8-flash-next"
        question_id = "msg_compact_release_question"
        prompt = "Return only the publication revision number from the archived release report."
        question = dict(type="message", id=question_id, role="user", status="completed",
                        content=[dict(type="input_text", text=prompt)])
        inputs = [dict(role="assistant", content=params["report"]), question]
        raw = owner.responses.with_raw_response.compact(
            model=model, previous_response_id=params["source"], input=inputs,
            instructions="Write a short handoff. Keep the exact publication revision from the report.")
        window = capture("compact", raw)
        ids = check_window(window, params["original_user"], question, params["error"],
                           params["legacy_error"])
        try:
            raw.parse()
            strict = "parsed"
        except openai.APIResponseValidationError:
            strict = "type_rejected"

        typed_window = CompactWindow.model_validate(window, strict=True)
        if typed_window.to_dict() != window:
            raise ValueError("Typed compact resource changed original HTTP fields")
        replay = dict(model=model, input=typed_window.to_dict()["output"], max_output_tokens=96,
                      temperature=0, reasoning={"effort": "none"}, store=False)
        try:
            peer.responses.create(**replay)
        except openai.BadRequestError as error:
            if error.code != "invalid_encrypted_content":
                raise
        else:
            raise ValueError("Another customer opened the sealed window")

        resumed = owner.responses.with_raw_response.create(**replay)
        answer = capture("resumed", resumed)
        if answer["status"] != "completed" or any(
                item["type"] == "mcp_call" for item in answer["output"]):
            raise ValueError("Unexpected continuation lifecycle or tool call")
        text = resumed.parse().output_text.strip()
        video = None
        if "video" in params:
            video = verify_video(owner, model, params["video"], capture)
        return dict(sdk=openai.__version__, source=params["source"], original_ids=ids,
                    output_items=len(window["output"]), usage=window["usage"],
                    raw_compact=True, typed_window_round_trip=True, strict_compact=strict, foreign_status=400,
                    foreign_code="invalid_encrypted_content", answer=text,
                    exact_answer=(text == str(params["revision"])), video=video, files=hashes, vary=varies)


if __name__ == "__main__":
    params = json.load(sys.stdin)
    execute = verify_saved_window if params.get("operation") == "replay" else run
    print(json.dumps(execute(params)))
