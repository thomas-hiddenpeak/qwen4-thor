#!/usr/bin/env python3
"""Verification harness: C++ NVFP4 engine vs transformers 5.16.1 reference.

固化 Phase 1 的 "L2 噪声保真度" 验证为可重复的验证标准体系 (Phase 2 完成
标准 "验证标准体系落地")。两侧在同一 token 序列上比较 prefill logits,
判据问 "差异是否只有量化噪声、有无系统性 bug", 而非 "logits 是否逐位一致"
(永远不可能 — C++ 把激活也量化到 e2m1 4-bit)。

判据 (l2_compare.py):
  [A] 每个 CONFIDENT 位置 (参考 top1-top2 gap > tau=3*std(delta)) argmax 全对;
  [B] 每个 argmax 翻转都是 near-tie (gap <= tau);
  [C] l2_rel 在 W4A4 噪声带 (mean ~0.2, max < 0.75)。

Pipeline:
  1. (可选) encode prompt text -> token ids (transformers tokenizer, 与 C++
     BPE 同源 tokenizer.json)。
  2. C++ dump:  q4t_tests model_forward_dump_decode
       env: Q4T_MODEL_LAYERS, Q4T_DECODE_STEPS=0, Q4T_DECODE_PROMPT_FILE,
            Q4T_DECODE_OUT -> <out>.prefill.bin (T*vocab f32)
  3. ref dump:  ref4_logits.py (lazy per-layer dequant, 支持全 48 层)
       env: Q4T_REF_LAYERS, Q4T_REF_PROMPT_FILE -> <out>.logits.npy
  4. compare:   l2_compare.py <cpp>.prefill.bin <ref>.logits.npy

Usage:
  python3 verify_logits.py --prompt "The capital of France is" \
      --layers 48 --tokens 256 --out /tmp/verify48
  python3 verify_logits.py --ids-file /tmp/prompt.txt --layers 48
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
MODEL_DIR = os.environ.get(
    "Q4T_MODEL_DIR",
    os.path.expanduser(
        "~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream"))
VENV_PY = os.path.join(REPO, "reference", ".venv-tokenizers", "bin", "python")
REF_DUMP = os.path.join(HERE, "ref_dump.py")
L2_COMPARE = os.path.join(HERE, "compare_logits.py")
Q4T_TESTS = os.path.join(REPO, "build", "q4t_tests")


def encode_prompt(text, max_tokens):
  """Encode with the model's tokenizer (same tokenizer.json as the C++ BPE)."""
  import transformers
  tok = transformers.AutoTokenizer.from_pretrained(MODEL_DIR)
  ids = tok.encode(text, add_special_tokens=False)
  if len(ids) > max_tokens:
    ids = ids[:max_tokens]
  return ids


def write_ids(ids, path):
  with open(path, "w") as f:
    for i in ids:
      f.write(f"{i}\n")


def run_cpp_dump(layers, out_prefix, prompt_file):
  env = dict(os.environ)
  env["Q4T_MODEL_LAYERS"] = str(layers)
  env["Q4T_DECODE_STEPS"] = "0"  # prefill only
  env["Q4T_DECODE_PROMPT_FILE"] = prompt_file
  env["Q4T_DECODE_OUT"] = out_prefix
  cmd = [Q4T_TESTS, "model_forward_dump_decode"]
  print(f"[cpp] {' '.join(cmd)} (layers={layers})", flush=True)
  r = subprocess.run(cmd, cwd=REPO, env=env)
  if r.returncode != 0:
    print(f"[cpp] FAILED rc={r.returncode}", file=sys.stderr)
    return False
  return os.path.exists(out_prefix + ".prefill.bin")


def run_ref_dump(layers, out_prefix, prompt_file):
  env = dict(os.environ)
  env["Q4T_REF_LAYERS"] = str(layers)
  env["Q4T_REF_PROMPT_FILE"] = prompt_file
  cmd = [VENV_PY, REF_DUMP, out_prefix]
  print(f"[ref] {' '.join(cmd)} (layers={layers})", flush=True)
  r = subprocess.run(cmd, cwd=REPO, env=env)
  if r.returncode != 0:
    print(f"[ref] FAILED rc={r.returncode}", file=sys.stderr)
    return False
  return os.path.exists(out_prefix + ".logits.npy")


def run_compare(cpp_bin, ref_npy):
  cmd = [VENV_PY, L2_COMPARE, cpp_bin, ref_npy]
  print(f"[cmp] {' '.join(cmd)}", flush=True)
  return subprocess.run(cmd, cwd=REPO).returncode


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--prompt", help="prompt text (encoded with the tokenizer)")
  ap.add_argument("--ids-file", help="file with token ids (one per line)")
  ap.add_argument("--layers", type=int, default=48)
  ap.add_argument("--tokens", type=int, default=256,
                  help="max prompt tokens when --prompt is given")
  ap.add_argument("--out", default="/tmp/verify")
  args = ap.parse_args()

  if not os.path.exists(Q4T_TESTS):
    print(f"q4t_tests not found at {Q4T_TESTS}; build first.", file=sys.stderr)
    return 1
  if not os.path.exists(REF_DUMP):
    print(f"ref dump not found at {REF_DUMP}", file=sys.stderr)
    return 1

  prompt_file = args.out + ".prompt.txt"
  if args.ids_file:
    import shutil
    shutil.copyfile(args.ids_file, prompt_file)
    with open(prompt_file) as f:
      n = len([l for l in f if l.strip()])
    print(f"prompt: {n} tokens from {args.ids_file}")
  elif args.prompt:
    ids = encode_prompt(args.prompt, args.tokens)
    write_ids(ids, prompt_file)
    print(f"prompt: encoded {len(ids)} tokens from {args.prompt!r}")
  else:
    print("need --prompt or --ids-file", file=sys.stderr)
    return 1

  if not run_cpp_dump(args.layers, args.out, prompt_file):
    return 1
  if not run_ref_dump(args.layers, args.out, prompt_file):
    return 1
  rc = run_compare(args.out + ".prefill.bin", args.out + ".logits.npy")
  print("\n=== VERDICT ===")
  print("PASS" if rc == 0 else "FAIL")
  return rc


if __name__ == "__main__":
  sys.exit(main())
