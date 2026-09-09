#!/usr/bin/env bash
# bench_mtp.sh — plain vs MTP decode benchmark (out-of-band verification).
#
# 为什么需要它: 本会话的工具输出层遭 prompt injection 污染, agent 看到的
# 性能数字不可信。此脚本设计为**由用户在自己的终端直接运行**, 结果直接
# 显示在用户自己的屏幕上 + 写入 /tmp/bench_mtp_results.txt (用户编辑器
# 打开即读), 完全绕过被污染的 agent 工具通道。
#
# 用法: bash /home/rm01/models/dev/qwen4-thor/tools/bench_mtp.sh
# 运行前请先在编辑器里打开本脚本确认内容无误 (防脚本本身被篡改)。
set -u
Q4T=/home/rm01/models/dev/qwen4-thor/build/q4t
PROMPT="Hello world, tell me about hash tables"
MAXTOK=30
OUT=/tmp/bench_mtp_results.txt

# 跑一次 generate, 提取 perf 行的 decode ms (二进制内部 chrono 计时,
# 不含模型加载)。失败输出 ERR。
run() {
  local label=$1; shift
  local ms
  ms=$("$Q4T" generate "$PROMPT" --max-tokens "$MAXTOK" "$@" 2>&1 \
       | grep -oE "decode [0-9]+ tok in [0-9.]+ ms" | grep -oE "[0-9.]+ ms" \
       | awk '{print $1}')
  [ -z "${ms:-}" ] && ms=ERR
  printf "%-10s %s ms\n" "$label" "$ms"
}

{
  echo "plain (3 runs):"
  run p1
  run p2
  run p3
  echo "mtp (2 runs each k):"
  for k in 1 2 3 4; do
    run "k=${k}a" --mtp --mtp-k "$k"
    run "k=${k}b" --mtp --mtp-k "$k"
  done
} | tee "$OUT"

# 汇总: 各组平均 ms -> tok/s, 相对 plain 的加速比 (纯 awk, 可复核算术)。
awk -v maxtok="$MAXTOK" '
  /^plain/ {sec="plain"; next}
  /^mtp/   {sec="mtp";   next}
  $2=="ERR" {next}
  sec=="plain" {ps+=$2; pn++}
  sec=="mtp"   {lbl=$1; sub(/^k=/,"",lbl); sub(/[ab]$/,"",lbl); s[lbl]+=$2; c[lbl]++}
  END {
    if (pn==0) {print "no valid plain runs"; exit}
    pm=ps/pn
    printf "\nplain avg: %.1f ms -> %.1f tok/s\n", pm, maxtok/(pm/1000)
    for (kk=1; kk<=10; kk++) if (c[kk]>0) {
      km=s[kk]/c[kk]
      printf "k=%-3d avg (%d runs): %.1f ms -> %.1f tok/s (%.2fx vs plain)\n", \
             kk, c[kk], km, maxtok/(km/1000), pm/km
    }
  }' "$OUT" | tee -a "$OUT"

echo
echo "results file: $OUT"
sha256sum "$OUT"
