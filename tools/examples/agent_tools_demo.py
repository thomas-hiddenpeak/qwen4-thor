"""Bounded messages/tool round trip against an explicitly selected Q4T server.

Running this script sends real generation requests. Development only for now;
see docs/AGENT_TOOL_CLIENT_2026-11-02.md. It never starts or stops a server.
"""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import secrets

from chat_client import (ChatClient, append_final, append_tool_results,
                         make_request, prepare_tool_calls, require)


ROOT = Path(__file__).resolve().parents[2]
TOOL = {"type": "function", "function": {
    "name": "lookup", "description": "Read an authoritative record by key.",
    "parameters": {"type": "object", "properties": {
        "key": {"type": "string", "enum": ["audit"]}},
        "required": ["key"], "additionalProperties": False}}}
RECORDS = {"audit": {"code": "831907"}}


def new_file(path, binary=False):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "wb" if binary else "w",
                     **({} if binary else {"encoding": "utf-8"}))


def save(path, value):
    with new_file(path) as output:
        json.dump(value, output, ensure_ascii=False, allow_nan=False, indent=2)
        output.write("\n")


def validate_lookup(arguments):
    require(set(arguments) == {"key"} and
            isinstance(arguments["key"], str) and
            arguments["key"] in RECORDS, "lookup expects key=audit")


def run(directory, base_url, *, model="qwen3.8-flash-next", stream=True,
        thinking=False, session_cache=False, max_rounds=4, max_tool_calls=8):
    require(type(max_rounds) is int and 1 <= max_rounds <= 8,
            "max_rounds must be between 1 and 8")
    require(type(max_tool_calls) is int and 1 <= max_tool_calls <= 32,
            "max_tool_calls must be between 1 and 32")
    directory = Path(directory).resolve()
    require(directory.is_relative_to(ROOT / ".q4t-work"),
            "use a new output directory inside .q4t-work")
    client = ChatClient(base_url)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    session_id = secrets.token_hex(32) if session_cache else None
    save(directory / "run-started.json", dict(base_url=base_url, model=model,
        max_requests=max_rounds, max_tool_calls=max_tool_calls,
        session_id=session_id, stream=stream, thinking=thinking))
    history = [{"role": "user", "content":
        "请先调用lookup工具，key必须是audit。拿到工具结果后，"
        "只回复其中code字段的六位数字。在工具返回之前不要猜测答案。"}]
    calls = 0
    try:
        for turn in range(max_rounds):
            label = f"round-{turn:02d}"
            request = make_request(history, [TOOL], model=model, stream=stream,
                thinking=thinking, session_id=session_id)
            save(directory / (label + "-request.json"), request)
            with new_file(directory / (label + "-wire.bin"), True) as wire:
                completion = client.complete(request, sink=wire.write)
            save(directory / (label + "-response.json"), asdict(completion))
            if completion.finish_reason == "stop":
                history = append_final(history, completion)
                save(directory / (label + "-history.json"), history)
                answer = completion.message["content"]
                require(calls > 0 and isinstance(answer, str) and
                        answer.strip() == RECORDS["audit"]["code"],
                        "the example did not complete the lookup task")
                save(directory / "completed.json", dict(requests=turn + 1,
                    tool_calls=calls, messages=history))
                return answer
            # A length-limited turn is retained above, never executed/committed.
            planned = prepare_tool_calls(completion, {"lookup": validate_lookup})
            require(turn + 1 < max_rounds, "no request budget for tool results")
            require(calls + len(planned) <= max_tool_calls,
                    "tool execution budget exceeded")
            save(directory / (label + "-plan.json"),
                 [asdict(invocation) for invocation in planned])
            results = {}
            for index, invocation in enumerate(planned):
                tool_label = f"{label}-tool-{index:03d}"
                save(directory / (tool_label + "-started.json"),
                     asdict(invocation))
                # Explicit read-only implementation; no eval, shell, or dispatch
                # to a model-specified module, executable, path, or network URL.
                value = RECORDS[invocation.arguments["key"]]
                content = json.dumps(value, ensure_ascii=False, allow_nan=False)
                save(directory / (tool_label + "-result.json"),
                     dict(tool_call_id=invocation.call_id, content=content))
                results[invocation.call_id] = content
                calls += 1
            history = append_tool_results(history, completion, results)
            save(directory / (label + "-history.json"), history)
    except BaseException as error:
        try:
            save(directory / "failed.json", dict(error=repr(error),
                 completed_tool_calls=calls, automatic_retry=False))
        except OSError as receipt_error:
            error.add_note("Could not save failure receipt: " + repr(receipt_error))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--model", default="qwen3.8-flash-next")
    parser.add_argument("--no-stream", action="store_true")
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--session-cache", action="store_true")
    parser.add_argument("--max-rounds", type=int, default=4)
    parser.add_argument("--max-tool-calls", type=int, default=8)
    args = parser.parse_args()
    print(run(args.directory, args.base_url, model=args.model,
              stream=not args.no_stream, thinking=args.thinking,
              session_cache=args.session_cache, max_rounds=args.max_rounds,
              max_tool_calls=args.max_tool_calls))


if __name__ == "__main__":
    main()
