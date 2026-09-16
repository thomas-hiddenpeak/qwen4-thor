#!/usr/bin/env python3
"""Concurrency load test for the q4t serve continuous-batching scheduler.

Fires C concurrent /v1/chat/completions requests and reports aggregate
tokens/s. This measures whether the engine's batched-decode scaling (bench-
decode: B=1->128 = 16.6->373 tok/s) is realized end-to-end through the HTTP
server + tokenizer + scheduler.
"""
import argparse
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def one_request(port, prompt, max_tokens):
    body = json.dumps({
        "model": "q4t",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=1200) as r:
        resp = json.loads(r.read())
    dt = time.time() - t0
    toks = resp.get("usage", {}).get("completion_tokens", 0)
    return toks, dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--requests", type=int, default=0)  # 0 => == concurrency
    args = ap.parse_args()
    n = args.requests or args.concurrency
    prompts = [
        f"Write a short paragraph about the number {i} and an adventurous robot."
        for i in range(n)
    ]
    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [ex.submit(one_request, args.port, prompts[i], args.max_tokens)
                for i in range(n)]
        for f in futs:
            results.append(f.result())
    wall = time.time() - t0
    total = sum(r[0] for r in results)
    avg_lat = sum(r[1] for r in results) / len(results)
    print(f"concurrency={args.concurrency} requests={n} "
          f"max_tokens={args.max_tokens}")
    print(f"  total completion tokens : {total}")
    print(f"  wall time               : {wall:.2f}s")
    print(f"  aggregate throughput    : {total / wall:.1f} tok/s")
    print(f"  per-request avg latency : {avg_lat:.2f}s "
          f"({total / len(results) / avg_lat:.2f} tok/s/req)")


if __name__ == "__main__":
    main()
