"""Prepare or send a five-request agent/cache example; preparation is CPU only.

See docs/AGENT_CACHE_INTEGRATION.md. This is an orchestration example, not an
agent SDK, tool-call parser, or performance acceptance workload.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import secrets
import urllib.error
import urllib.request


MODEL = "qwen3.8-flash-next"
MODEL_DIR = Path.home() / "models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream"
ASSISTANT = "<|im_start|>assistant\n<think>\n\n</think>\n\n"
OUTPUT_TOKENS = 512
REQUEST_MS = 1_200_000
CAPACITIES = [208896, 65536, 65536, 65536]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def tokenizer(model_dir):
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)


def ids(tok, text):
    return tok.encode(text, add_special_tokens=False)


def user_turn(text):
    return "<|im_end|>\n<|im_start|>user\n" + text + "<|im_end|>\n" + ASSISTANT


def pad_to(tok, front, tail, target):
    # Demo material only. Real integration supplies its own canonical history.
    line = "Archived project note: preserve API behavior and review changes.\n"
    low, high = 0, target // max(1, len(ids(tok, line))) + 32
    while len(ids(tok, front + line * high + tail)) < target:
        high *= 2
    while low < high:
        mid = (low + high) // 2
        if len(ids(tok, front + line * mid + tail)) < target:
            low = mid + 1
        else:
            high = mid
    return front + line * low + tail


def prepare(directory, model_dir, boundary):
    if directory.exists():
        raise ValueError("Use a new directory; existing client state is preserved")
    tok = tokenizer(model_dir)
    system = ("<|im_start|>system\nYou are a software engineering assistant. "
              "The coordinator runs tools and supplies their results.\n"
              "<|im_end|>\n<|im_start|>user\nShared project context:\n"
              "Available coordinator operations: read_file(path), "
              "review_patch(diff), run_tests(target).\n")
    common = pad_to(tok, system, "\nEnd shared project context.\n", boundary + 64)
    tasks = [
        "Main agent: outline a plan to implement a bounded LRU cache.",
        "Child 1: propose the LRU cache API and invariants.",
        "Child 2: review cache concurrency and cancellation behavior.",
        "Child 3: propose cache boundary and recovery tests.",
    ]
    prompts = []
    for i, target in enumerate([98304, max(33792, boundary + 1024), 49152, 60000]):
        tail = "\n" + tasks[i] + " Keep the reply concise.<|im_end|>\n" + ASSISTANT
        prompts.append(pad_to(tok, common + f"Private history for agent {i}:\n",
                              tail, target))
    token_rows = [ids(tok, text) for text in prompts]
    if not all(row[:boundary] == token_rows[0][:boundary] for row in token_rows):
        raise ValueError("Shared text did not produce the required exact token prefix")
    directory.mkdir(parents=True, mode=0o700)
    agents = []
    for i, (prompt, row) in enumerate(zip(prompts, token_rows)):
        if len(row) + OUTPUT_TOKENS > CAPACITIES[i]:
            raise ValueError("Input plus output exceeds the intended slot capacity")
        filename = f"agent-{i}.prompt.txt"
        (directory / filename).write_text(prompt)
        agents.append(dict(session_id=secrets.token_hex(32), prompt=filename,
                           prompt_sha256=digest(prompt), input_tokens=len(row),
                           capacity=CAPACITIES[i]))
    state = dict(model_dir=str(model_dir.resolve()), boundary=boundary,
                 prefix_cache_scope=secrets.token_hex(32), agents=agents)
    save(directory / "state.json", state)
    print("Prepared input token counts:", [a["input_tokens"] for a in agents])
    print("No HTTP sent. Client state:", directory / "state.json")


def read_sse(response):
    text, usage, finish, done = [], None, None, False
    for raw in response:
        if not raw.startswith(b"data:"):
            continue
        data = raw[5:].strip()
        if data == b"[DONE]":
            done = True
            continue  # Drain to EOF; do not close immediately at [DONE].
        event = json.loads(data)
        if "error" in event:
            raise RuntimeError("Stream failed: " + repr(event["error"]))
        if event.get("usage") is not None:
            usage = event["usage"]
        for choice in event.get("choices", []):
            text.append(choice.get("delta", {}).get("content", ""))
            if choice.get("finish_reason") is not None:
                finish = choice["finish_reason"]
    if not done or usage is None or finish not in ("stop", "length"):
        raise RuntimeError("Incomplete or cancelled response; do not commit this turn")
    return dict(text="".join(text), usage=usage, finish_reason=finish)


def chat(base_url, directory, label, prompt, session_id, scope, boundary=0):
    body = dict(model=MODEL, prompt=prompt, max_tokens=OUTPUT_TOKENS,
                stream=True, stream_options=dict(include_usage=True),
                temperature=0, top_p=1, n=1,
                session_id=session_id, prefix_cache_scope=scope,
                prefix_cache_write_tokens=boundary,
                request_id="agent-" + secrets.token_hex(16),
                cancel_token=secrets.token_hex(32), request_timeout_ms=REQUEST_MS)
    save(directory / f"{label}.request.json", body)
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    # Socket timeout is larger than the server's queue+generation deadline.
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_MS / 1000 + 60) as response:
            result = read_sse(response)
    except urllib.error.HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")
        save(directory / f"{label}.error.json", dict(status=error.code, body=message))
        raise RuntimeError(f"HTTP {error.code}: {message}") from error
    save(directory / f"{label}.response.json", result)
    cached = result["usage"].get("prompt_tokens_details", {}).get("cached_tokens", 0)
    print(f"{label}: cached_tokens={cached}, finish={result['finish_reason']}", flush=True)
    return result


def run(directory, base_url):
    state = json.loads((directory / "state.json").read_text())
    tok = tokenizer(Path(state["model_dir"]))
    agents, scope, boundary = state["agents"], state["prefix_cache_scope"], state["boundary"]
    prompts = [(directory / agent["prompt"]).read_bytes().decode("utf-8")
               for agent in agents]
    for agent, prompt in zip(agents, prompts):
        if digest(prompt) != agent["prompt_sha256"]:
            raise ValueError("Prompt changed after preparation; prepare a new group")
    # Do not silently replay partially completed requests after an error.
    with (directory / "run-started.json").open("x") as marker:
        json.dump(dict(base_url=base_url, planned_requests=5), marker)
    source = chat(base_url, directory, "main-seed", prompts[0],
                  agents[0]["session_id"], scope, boundary)
    # Source HTTP has ended. Main holds no execution slot while waiting.
    def child(i):
        return chat(base_url, directory, f"child-{i}", prompts[i],
                    agents[i]["session_id"], scope)
    with ThreadPoolExecutor(max_workers=3) as pool:
        children = list(pool.map(child, [1, 2, 3]))
    reports = "\n\n".join(f"Child {i}:\n{r['text']}"
                             for i, r in enumerate(children, 1))
    resumed_prompt = prompts[0] + source["text"] + user_turn(
        "Coordinator tool/subagent results:\n" + reports +
        "\nCombine these results into the final implementation plan.")
    if len(ids(tok, resumed_prompt)) + OUTPUT_TOKENS > CAPACITIES[0]:
        raise ValueError("Main continuation exceeds long-slot capacity")
    resumed = chat(base_url, directory, "main-resumed", resumed_prompt,
                   agents[0]["session_id"], scope)
    save(directory / "completed.json", dict(
        requests=5, main_history=resumed_prompt + resumed["text"],
        child_histories=[p + r["text"] for p, r in zip(prompts[1:], children)]))
    print("Completed. Keep each original prompt and full response text for the next turn.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    parser.add_argument("--boundary", type=int, choices=[8192, 32768], default=8192)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.directory, args.model_dir, args.boundary)
    else:
        run(args.directory, args.base_url)


if __name__ == "__main__":
    main()
