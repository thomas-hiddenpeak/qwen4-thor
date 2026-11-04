#!/usr/bin/env python3
"""Bounded observations of current serve behavior, NOT a release acceptance.

Run under gpu_exclusive.py because the probe exercises production HTTP code.
It uses socketpairs, synthetic outputs, and an unloaded backend; zero model
requests, listeners, CUDA calls, or writes to the installed release.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixtures():
    rows = []
    base = {"messages": [{"role": "user", "content": "query project"}],
            "enable_thinking": False}
    schema = {"type": "object", "properties": {"query": {"type": "string"}},
              "required": ["query"], "additionalProperties": False}
    tool = {"type": "function", "function": {"name": "lookup",
                                              "parameters": schema}}
    generated = "<tool_call>\n<function=lookup>\n</function>\n</tool_call>"

    def chat(name, request, expected, finding=None, output=None):
        row = {"case": name, "op": "chat", "request": request,
               "expected_current": expected, "finding": finding}
        if output is not None:
            row["generated"] = output
        rows.append(row)

    req = copy.deepcopy(base)
    req["tools"] = [copy.deepcopy(tool)]
    req["tools"][0]["strict"] = True
    chat("outer_strict_silently_unenforced", req,
         {"contract": True, "parser_init": True, "constrained": False,
          "finish": True}, "F1", generated)
    req = copy.deepcopy(base)
    req["tools"] = [copy.deepcopy(tool)]
    req["tools"][0]["function"]["strict"] = True
    chat("correct_strict_rejects_missing_argument", req,
         {"contract": True, "constrained": True, "finish": False},
         output=generated)
    req = copy.deepcopy(base)
    req["tools"] = [copy.deepcopy(tool)]
    req["tools"][0]["function"]["paramaters"] = \
        req["tools"][0]["function"].pop("parameters")
    chat("misspelled_parameters_accepted", req,
         {"contract": True, "parser_init": True, "constrained": False,
          "finish": True}, "F1", generated)

    call = {"role": "assistant", "tool_calls": [
        {"id": "call-a", "type": "function", "function":
         {"name": "lookup", "arguments": '{"query":"AUDIT_SENTINEL"}'}}]}
    result = {"role": "tool", "tool_call_id": "call-a", "content": "found"}
    req = copy.deepcopy(base)
    req["messages"] += [copy.deepcopy(call), copy.deepcopy(result)]
    chat("identified_history_control", req,
         {"contract": True, "render": True, "parser_init": True,
          "prompt_contains": "AUDIT_SENTINEL"})
    req = copy.deepcopy(req)
    function = req["messages"][1]["tool_calls"][0]["function"]
    function["argumens"] = function.pop("arguments")
    chat("history_arguments_typo_lost", req,
         {"contract": True, "render": True, "parser_init": True,
          "prompt_excludes": "AUDIT_SENTINEL"}, "F2")
    req = copy.deepcopy(base)
    req["messages"] += [copy.deepcopy(call), copy.deepcopy(result)]
    del req["messages"][2]["content"]
    chat("missing_tool_result_content_accepted", req,
         {"contract": True, "render": True, "parser_init": True}, "F2")
    req = copy.deepcopy(base)
    legacy = copy.deepcopy(call)
    del legacy["tool_calls"][0]["id"]
    legacy["tool_calls"][0]["type"] = "custom"
    req["messages"] += [legacy, {"role": "tool", "content": "legacy"}]
    chat("legacy_unknown_type_rendered_as_function", req,
         {"contract": True, "render": True,
          "prompt_contains": "<function=lookup>"}, "F2")
    req = copy.deepcopy(base)
    bad_call = copy.deepcopy(call)
    bad_call["tool_calls"][0]["type"] = "custom"
    req["messages"] += [bad_call, copy.deepcopy(result)]
    chat("identified_custom_type_rejected", req, {"contract": False})
    for field in ("temperature", "top_p", "n", "frequency_penalty",
                  "presence_penalty", "stream", "stream_options", "user"):
        req = copy.deepcopy(base)
        req[field] = None
        chat("nullable_" + field, req, {"contract": False}, "F3")
    req = copy.deepcopy(base)
    req.update(temperature=0, top_p=1, n=1, stream=False)
    chat("neutral_parameters_control", req,
         {"contract": True, "parser_init": True})
    req = copy.deepcopy(base)
    req["max_completion_tokens"] = None
    req["max_tokens"] = 10
    chat("nullable_budget_alias_control", req,
         {"contract": True, "parser_init": True})
    # Keep the original lexical JSON; Python binary64 would round it first.
    row = {"case": "fraction_rounded_into_integer_budget", "op": "chat",
           "request": dict(base, max_tokens="AUDIT_NUMBER_LITERAL"),
           "expected_current": {"contract": True, "parser_init": True},
           "finding": "F7"}
    rows.append(row)
    chat("ordinary_fraction_budget_rejected", dict(base, max_tokens=1.5),
         {"contract": False})

    def http(name, path, body="", method="POST", headers="", code=200,
             cancelled=False, finding=None):
        wire = (f"{method} {path} HTTP/1.1\r\nHost: audit\r\n"
                f"Content-Length: {len(body.encode())}\r\n{headers}\r\n{body}")
        rows.append({"case": name, "op": "http", "wire": wire,
                     "finding": finding, "expected_current": {
                         "status": code, "cancelled": cancelled}})

    cancel = {"request_id": "audit-request", "cancel_token": "a" * 64}
    http("cancel_unknown_control_accepted", "/v1/requests/cancel",
         json.dumps(dict(cancel, dry_run=True)), code=202, cancelled=True,
         finding="F4")
    http("cancel_control", "/v1/requests/cancel", json.dumps(cancel),
         code=202, cancelled=True)
    http("cancel_wrong_key", "/v1/requests/cancel",
         json.dumps(dict(cancel, cancel_token="b" * 64)), code=404)
    http("cancel_missing_id", "/v1/requests/cancel",
         json.dumps({"cancel_token": "a" * 64}), code=404, finding="F4")
    http("wrong_media_type_reaches_stub", "/v1/chat/completions", "{}",
         headers="Content-Type: text/plain\r\n", finding="G6")
    http("expect_rejected", "/v1/chat/completions", "{}",
         headers="Expect: 100-continue\r\n", code=400)
    http("transfer_encoding_rejected", "/v1/chat/completions", "{}",
         headers="Transfer-Encoding: chunked\r\n", code=400)
    http("wrong_method_is_404", "/v1/models", method="POST", code=404,
         finding="G6")
    http("query_string_is_404", "/v1/models?unused=1", method="GET",
         code=404, finding="G6")
    http("model_list_control", "/v1/models", method="GET")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--engine-build", type=Path, required=True)
    args = parser.parse_args()
    out, engine = args.output.resolve(), args.engine_build.resolve()
    if not any(out.is_relative_to(ROOT / p) for p in ("build", ".q4t-work")):
        parser.error("output must be under build/ or .q4t-work/")
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    sources = ["tools/serve_audit/probe.cpp", "src/io/json.cpp"] + [
        "src/server/" + name + ".cpp" for name in (
            "chat_http", "chat_metrics", "chat_capabilities", "http_request",
            "http_response", "input_contract", "chat_contract", "chat_template",
            "chat_tool_history", "chat_response_parser", "output_constraints",
            "output_schema")]
    # Reuse only the unchanged tokenizer static library / generated identity.
    # No XGrammar compiler or model inference is linked into this probe.
    command = ["g++-14", "-std=c++23", "-O1", "-Wall", "-Wextra", "-Werror",
               "-pthread", "-DQ4T_ENABLE_XGRAMMAR=1", "-Iinclude",
               "-I" + str(engine / "generated"),
               "-I/usr/local/cuda-13.3/targets/sbsa-linux/include", *sources,
               str(engine / "libq4t_text.a"), str(engine / "libq4t_io.a"),
               "-Wl,-rpath,/usr/local/cuda-13.3/targets/sbsa-linux/lib",
               "/usr/local/cuda-13.3/targets/sbsa-linux/lib/libcudart.so",
               "-licui18n", "-licuuc",
               "-licudata", "-o", str(out / "probe")]
    (out / "build-command.json").write_text(json.dumps(command, indent=2) + "\n")
    built = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=60,
                           env=dict(os.environ, TMPDIR=str(out)))
    (out / "build.stdout").write_bytes(built.stdout)
    (out / "build.stderr").write_bytes(built.stderr)
    built.check_returncode()
    rows = fixtures()
    input_text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    input_text = input_text.replace('"AUDIT_NUMBER_LITERAL"',
                                    '1.0000000000000001')
    (out / "fixtures.jsonl").write_text(input_text)
    observed = subprocess.run([str(out / "probe")], cwd=ROOT,
                              input=input_text.encode(), capture_output=True,
                              timeout=20)
    (out / "observations.jsonl").write_bytes(observed.stdout)
    (out / "probe.stderr").write_bytes(observed.stderr)
    observed.check_returncode()
    outputs = [json.loads(line) for line in observed.stdout.splitlines()]
    if len(outputs) != len(rows):
        raise RuntimeError("incomplete probe output")
    checks = []
    for row, actual in zip(rows, outputs):
        if row["case"] != actual["case"]:
            raise RuntimeError("case ordering mismatch")
        observation = actual["observation"]
        comparisons = {}
        for key, value in row["expected_current"].items():
            if key == "status":
                matches = observation["wire"].startswith(f"HTTP/1.1 {value} ")
            elif key == "prompt_contains":
                matches = value in observation["prompt"]
            elif key == "prompt_excludes":
                matches = value not in observation["prompt"]
            else:
                matches = observation.get(key) == value
            comparisons[key] = matches
        checks.append({"case": row["case"], "finding": row["finding"],
                       "matches_current_observation": all(comparisons.values()),
                       "checks": comparisons})
    identity = {p: digest(ROOT / p) for p in sources}
    for path in (engine / "libq4t_text.a", engine / "libq4t_io.a",
                 engine / "generated/q4t/server/"
                 "chat_protocol_identity.h", ROOT / "build/q4t",
                 ROOT / "build/q4t.release.json"):
        identity[str(path.relative_to(ROOT))] = digest(path)
    result = {"purpose": "audit_reproduction_not_release_acceptance",
              "all_observations_match": all(
                  c["matches_current_observation"] for c in checks),
              "cases": len(checks), "checks": checks,
              "model_requests": 0, "listeners_started": 0,
              "synthetic_outputs": True, "runtime_fixes": False,
              "elapsed_seconds": time.monotonic() - started,
              "sources_and_dependencies_sha256": identity,
              "probe_sha256": digest(out / "probe")}
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: result[k] for k in (
        "purpose", "all_observations_match", "cases", "model_requests",
        "elapsed_seconds")}))
    if not result["all_observations_match"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
