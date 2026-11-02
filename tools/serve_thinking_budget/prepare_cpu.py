"""Record identity-qualified template reuse and tokenize the fixed cache fixture."""
import json
from pathlib import Path
import subprocess

import stage
v = stage.module("budget_probe_input", Path(__file__).with_name("verify.py"))


def main():
    cpu = stage.BASE / "cpu"
    body = v.cache_seed()
    body.update(body.pop("extra_body"))
    for key in ("thinking_token_budget", "reasoning_field", "include_reasoning"):
        body.pop(key)
    row = dict(request=body, contents=[m["content"] for m in body["messages"]])
    raw = json.dumps(row, ensure_ascii=False) + "\n"
    probe = stage.ROOT / ".q4t-work/serve-stage-b2a-20261103/probe/template_probe"
    model = Path(stage.h.read(stage.ROOT /
        ".q4t-work/serve-stage-b2a-20261103/package/plan.json")["model_dir"])
    for name in ("cache-probe-request.jsonl", "template-reuse.json"):
        assert not (cpu / name).exists(), "preserve existing evidence"
    (cpu / "cache-probe-request.jsonl").write_text(raw)
    run = subprocess.run([str(probe), str(model / "tokenizer.json")],
                         input=raw, text=True, capture_output=True, timeout=30)
    (cpu / "cache-probe-output.jsonl").write_text(run.stdout)
    (cpu / "cache-probe-stderr.log").write_text(run.stderr)
    assert run.returncode == 0
    result = json.loads(run.stdout)
    assert result["contract"] and result["rendered"] and result["profile"]
    assert 8192 <= len(result["ids"]) < 14000
    stage.h.save(cpu / "cache-probe-result.json", dict(passed=True,
        tokens=len(result["ids"]), probe=str(probe), probe_sha256=stage.h.sha(probe),
        tokenizer_sha256=stage.h.sha(model / "tokenizer.json"),
        note="Prior native renderer/tokenizer; new budget controls excluded from rendering-only probe. New native CPU contracts verify budget inputs."))
    old = stage.h.read(stage.ROOT / ".q4t-work/serve-stage-b2a-20261103/package/sources.json")
    paths = ("src/server/chat_template.cpp", "src/server/chat_tool_history.cpp",
             "src/server/chat_input.cpp", "src/text/tokenizer.cpp")
    checks = {path: stage.h.sha(stage.ROOT / path) for path in paths}
    assert all(old[path] == checks[path] for path in paths)
    previous = subprocess.check_output(["git", "show", "24c831da:src/server/reasoning_options.cpp"],
                                       cwd=stage.ROOT, text=True)
    current = (stage.ROOT / "src/server/reasoning_options.cpp").read_text()
    marker = "Status ResolveMessageReasoning("
    assert previous[previous.index(marker):] == current[current.index(marker):]
    prior = stage.ROOT / ".q4t-work/serve-stage-b2a-20261103/cpu/template-diff/result.json"
    assert stage.h.read(prior)["passed"]
    stage.h.save(cpu / "template-reuse.json", dict(passed=True, files=checks,
        history_function_exact=True, prior=str(prior), prior_sha256=stage.h.sha(prior),
        scope="120 prior cases; unchanged native template/tokenizer/history resolver. No new differential run claimed."))
    for prefix, count in (("driver", 8), ("client", 27)):
        log = cpu / ("driver-capture.log" if prefix == "driver" else "client.log")
        assert f"Ran {count} tests" in log.read_text() and log.read_text().endswith("\nOK\n")
        stage.h.save(cpu / (prefix + "-result.json"), dict(passed=True, tests=count,
                     log_sha256=stage.h.sha(log)))
    print("CPU metadata complete; cache seed", len(result["ids"]), "tokens")


if __name__ == "__main__":
    main()
