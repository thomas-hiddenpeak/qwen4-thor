"""Bounded CPU differential check; does not measure inference performance."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("parent", "candidate", "tokenizer", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    out = args.output.resolve()
    if not any(out.is_relative_to(root / p) for p in ("build", ".q4t-work")):
        raise ValueError("evidence must remain in build/ or .q4t-work/")
    out.mkdir(parents=True, exist_ok=False)
    markers = [t["content"] for t in json.loads(args.tokenizer.read_text())["added_tokens"]]
    cases = ["", "Hello, world!", "中文🚀🎉cafe\u0301\n\t", "a\x00b"]
    cases += markers
    cases += [m + markers[(i + 1) % len(markers)] for i, m in enumerate(markers)]
    cases += [m[:-1] + m + m[1:] for m in markers]
    rng = random.Random(20261030)
    fragments = markers + ["hello ", "中文 ", "🚀", "e\u0301", "\n\t", "<|im_", "123456", " "]
    cases += ["".join(rng.choice(fragments) for _ in range(24)) for _ in range(96)]
    cases += ["<|im_start|>" * n for n in (2000, 4000, 8000)]
    payload = "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases)
    (out / "inputs.jsonl").write_text(payload)
    result = {"passed": False, "cases": len(cases), "seed": 20261030,
              "scope": "CPU exact token IDs; not HTTP or performance acceptance"}
    answers = {}
    try:
        for name in ("parent", "candidate"):
            binary = getattr(args, name).resolve()
            start = time.monotonic()
            run = subprocess.run([str(binary), str(args.tokenizer.resolve())],
                                 input=payload, text=True, capture_output=True,
                                 timeout=180)
            (out / (name + ".jsonl")).write_text(run.stdout)
            (out / (name + ".stderr")).write_text(run.stderr)
            result[name] = {"binary": str(binary), "returncode": run.returncode,
                            "wall_seconds": time.monotonic() - start,
                            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
            if run.returncode:
                raise RuntimeError(name + " tokenizer failed")
            answers[name] = [json.loads(line) for line in run.stdout.splitlines()]
        assert len(answers["parent"]) == len(cases)
        assert len(answers["candidate"]) == len(cases)
        result["mismatched_cases"] = [i for i, (a, b) in enumerate(
            zip(answers["parent"], answers["candidate"])) if a != b]
        assert not result["mismatched_cases"], "token ID mismatch"
        result["passed"] = True
    finally:
        (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
