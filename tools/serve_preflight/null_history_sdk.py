"""Exercise a stored empty assistant turn and real release lookup via the SDK."""
from contextlib import ExitStack
import hashlib
import importlib.metadata
import json
from pathlib import Path
import ssl
import sys
from urllib.parse import urlsplit

from openai import APIResponseValidationError, BadRequestError, NotFoundError, OpenAI
try:
    import httpx2 as httpx
except ImportError:
    import httpx


def read_history(owner, peer, standard, chat_id, call_id, capture, phase):
    # The HTTP contract permits user, assistant and tool roles in one page.
    # The pinned Python SDK's optional strict model narrows role to assistant.
    raw = owner.chat.completions.messages.with_raw_response.list(chat_id, order="asc")
    body = raw.http_response.content
    page = json.loads(body)
    assert page["object"] == "list" and page["has_more"] is False
    saved = page["data"]
    assert [m["role"] for m in saved] == ["user", "assistant", "user"]
    assert all(set(("id", "content", "content_parts")) <= set(m) for m in saved)
    assert [page["first_id"], page["last_id"]] == [saved[0]["id"], saved[-1]["id"]]
    try:
        raw.parse()
    except APIResponseValidationError as error:
        problems = error.__cause__.errors()
        assert {(tuple(e["loc"][-2:]), e["input"]) for e in problems} == {
            ((0, "role"), "user"), ((2, "role"), "user")}
        strict_mixed_roles = False
    else:
        strict_mixed_roles = True
    parsed = standard.chat.completions.messages.list(chat_id, order="asc")
    assert [(v.role, v.content, v.id) for v in parsed.data] == [
        (v["role"], v["content"], v["id"]) for v in saved]
    scoped = owner.chat.completions.messages.list(chat_id, after=saved[0]["id"], limit=1)
    assert len(scoped.data) == 1 and scoped.data[0].id == saved[1]["id"]
    assert scoped.data[0].content is None and "content" in scoped.data[0].model_fields_set
    empty = saved[1]
    assert "content" in empty and empty["content"] is None
    assert saved[0]["content"] == "I will ask about a project release."
    assert saved[2]["content"].startswith("Use lookup_release")
    chat = owner.chat.completions.retrieve(chat_id)
    assert chat.choices[0].message.tool_calls[0].id == call_id
    try:
        peer.chat.completions.messages.list(chat_id)
    except NotFoundError as error:
        assert error.status_code == 404
    else:
        raise AssertionError("Another key retrieved the original history")
    (capture/(phase+"-history.json")).write_bytes(body)
    return dict(chat_id=chat_id, call_id=call_id, empty_id=empty["id"],
                history_sha256=hashlib.sha256(body).hexdigest(), peer=404,
                strict_empty_page=True, strict_mixed_page=strict_mixed_roles)


def main():
    cfg = json.load(sys.stdin)
    capture = Path(cfg["capture"])
    capture.mkdir(mode=0o700)
    address = urlsplit(cfg["base"])
    assert address.hostname == "127.0.0.1"
    if address.scheme == "https":
        verify = ssl.create_default_context(cafile=cfg["certificate"])
    else:
        assert address.scheme == "http" and cfg.get("handoff") and not cfg["certificate"]
        verify = True
    release = Path(cfg["release_record"])
    revision = cfg["revision"]
    tool = dict(type="function", function=dict(name="lookup_release",
        description="Read the project's release record for this revision.",
        parameters=dict(type="object", properties=dict(revision=dict(type="integer")),
                        required=["revision"], additionalProperties=False)))
    request = [dict(role="user", content="I will ask about a project release."),
               dict(role="assistant", content=None),
               dict(role="user", content=f"Use lookup_release to read revision {revision}. "
                    "After the tool returns, answer only the number of protected files.")]
    common = dict(model="qwen3.8-flash-next", temperature=0, reasoning_effort="none",
                  max_tokens=128)
    restored = None
    with ExitStack() as resources:
        def client(key, strict=True):
            return resources.enter_context(OpenAI(base_url=cfg["base"], api_key=key,
                max_retries=0, _strict_response_validation=strict,
                http_client=httpx.Client(verify=verify, timeout=60)))
        owner, peer = client(cfg["key"]), client(cfg["other"])
        standard = client(cfg["key"], strict=False)
        if previous := cfg.get("previous"):
            restored = read_history(owner, peer, standard, previous["chat_id"], previous["call_id"],
                                    capture, "restored")
            assert restored == previous
        try:
            owner.chat.completions.create(**common, messages=[
                request[0], {"role":"assistant"}, request[-1]])
        except BadRequestError as error:
            assert error.status_code == 400 and error.param == "messages[1].content"
        else:
            raise AssertionError("Missing assistant payload accepted")
        response = owner.chat.completions.create(**common, store=True, messages=request,
            tools=[tool], tool_choice={"type":"function","function":{"name":"lookup_release"}})
        (capture/"lookup.json").write_text(response.model_dump_json(exclude_unset=True))
        choice = response.choices[0]
        assert choice.finish_reason == "tool_calls" and len(choice.message.tool_calls) == 1
        call = choice.message.tool_calls[0]
        assert call.id and call.function.name == "lookup_release"
        args = json.loads(call.function.arguments)
        assert args == {"revision":revision} and type(args["revision"]) is int
        # Read the actual file only after the model chooses its lookup argument.
        raw = release.read_bytes()
        publication = json.loads(raw)["publication"]
        assert publication["publication_revision"] == args["revision"] and publication["status"] == "PASS"
        value = publication["protected_files"]
        assert type(value) is int
        saved = read_history(owner, peer, standard, response.id, call.id, capture, "original")
        submitted = [*request, dict(role="assistant", content=None, tool_calls=[
            call.model_dump(exclude_unset=True)]), dict(role="tool", tool_call_id=call.id,
            content=json.dumps(dict(revision=revision,protected_files=value)))]
        chunks, text, finishes, usages = [], [], [], []
        with owner.chat.completions.create(**common, tools=[tool], tool_choice="none",
                messages=submitted, stream=True, stream_options={"include_usage":True}) as stream:
            for part in stream:
                chunks.append(part.model_dump_json(exclude_unset=True))
                if part.usage is not None:
                    usages.append(part.usage.total_tokens)
                for item in part.choices:
                    if item.delta.content:
                        text.append(item.delta.content)
                    if item.finish_reason:
                        finishes.append(item.finish_reason)
        (capture/"chunks.jsonl").write_text("\n".join(chunks)+"\n")
        answer = "".join(text).strip()
        assert answer == str(value) and finishes == ["stop"] and len(usages) == 1 and usages[0] > 0
        audit = owner.chat.completions.create(**common, messages=[
            request[0],request[1],dict(role="user",content="The audit code is 720041. "
                "Return only the six digits.")])
        assert audit.choices[0].message.content.strip() == "720041"
    print(json.dumps(dict(sdk=importlib.metadata.version("openai"),
        revision=revision,source_sha256=hashlib.sha256(raw).hexdigest(),saved=saved,
        restored=restored,reply=answer,finishes=finishes,usage=usages[0],
        independent_chat="720041",missing_content_status=400,chunks=len(chunks))))


if __name__ == "__main__":
    main()
