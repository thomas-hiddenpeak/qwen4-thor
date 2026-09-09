#!/usr/bin/env bash
# profile_mtp.sh — nsys profile of the decode phase (MTP k=3 + plain).
#
# 为什么需要它: 本会话的工具输出层遭 prompt injection 污染, agent 看到的
# profiling 结果不可信。此脚本由用户在自有终端运行, 结果直接显示在用户
# 屏幕 + 写入 /tmp/profile_mtp_stats.txt (编辑器打开即读), 绕过污染通道。
#
# 机制: Q4T_PROFILE=1 让 q4t 用 cudaProfilerStart/Stop 框住 decode 阶段,
# nsys --capture-range=cudaProfilerApi 只抓 decode (不抓 ~15s 模型加载)。
# 输出三份统计: GPU kernel 耗时 / GPU 内存拷贝耗时 / CUDA API (CPU 侧) 耗时。
# 判读: 每 step wall ~128ms; 若 GPU busy (kernel+memcpy) 远小于 wall,
# 差额 = host 开销/同步空转 (即要找的 overhead)。
#
# 用法: bash /home/rm01/models/dev/qwen4-thor/tools/profile_mtp.sh
set -u
Q4T=/home/rm01/models/dev/qwen4-thor/build/q4t
PROMPT="Hello world, tell me about hash tables"
MAXTOK=30
OUT=/tmp/profile_mtp_stats.txt

run_profile() {
  local label=$1; shift
  local rep="/tmp/nsys_${label}"
  echo "=== profiling: $label ==="
  Q4T_PROFILE=1 nsys profile -o "$rep" \
      --capture-range=cudaProfilerApi --capture-range-end=stop -f true \
      "$Q4T" generate "$PROMPT" --max-tokens "$MAXTOK" "$@" 2>&1 \
      | grep -E "perf:|MTP:"
  echo "--- GPU kernel time (top 15) ---"
  nsys stats --report cuda_gpu_kern_sum "$rep.nsys-rep" 2>/dev/null | head -25
  echo "--- GPU memory copy time ---"
  nsys stats --report cuda_gpu_mem_time_sum "$rep.nsys-rep" 2>/dev/null | head -12
  echo "--- CUDA API time, CPU-side (top 15) ---"
  nsys stats --report cuda_api_sum "$rep.nsys-rep" 2>/dev/null | head -25
  echo
}

{
  run_profile mtp_k3 --mtp --mtp-k 3
  run_profile plain
} | tee "$OUT"

echo "stats file: $OUT"
sha256sum "$OUT"
