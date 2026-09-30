# MoE 分层专家驻留与按需加载 — 验收报告（DRAFT，矩阵执行中）

状态：2026-09-30 晚冻结轮全矩阵 E2E 执行中（基线 C=0 → 候选
C=256+hot-256 → 冻结口径对比）。本报告为骨架，结果节在矩阵完成后
按 compare-report.txt 与原始证据填写；在此之前任何"通过"字样均不
构成验收结论。计划与冻结口径见
[MOE_RESIDENCY_PLAN_2026-09-30.md](MOE_RESIDENCY_PLAN_2026-09-30.md)。

## 1. 目标与验收标准（/goal，2026-09-30）

单 Thor、单流、max_seq=1、文本 greedy、MTP 关、保持模型精度与计算
合同：

1. 总序列容量 262144，实际完成接近容量上限的长上下文请求；
2. 服务启动→模型加载→prefill/decode，推理服务及必要辅助进程物理
   内存峰值 ≤ 54,000,000,000 B（整机 RAM 与 swap 分列记录，不以
   swap 掩盖超支）；
3. 五档 HTTP E2E + 目标长度档，各档 decode 吞吐 ≥ 同条件全专家常驻
   基线的 50%（本专项门槛替代"性能不回退"）；
4. 数值合同、HTTP 输出、请求生命周期验收通过，保留可回退版本。

目标长度档口径（用户 2026-09-30 澄清）：**总上下文 = 输入 + 输出 =
262144**，取输入 261887 + 输出 257；261887/261888 边界调查关闭，
验收只看总上下文达标且服务端 token 计数完整、无截断。

## 2. 用户决策记录（2026-09-30，按时间顺序）

1. 目标长度档以总上下文 262144 为准（非"输入 256k"）；
2. 支持 C=256（每层 256 槽），即使内存预算超支也接受——验收报告
   如实报告实测峰值并标注本接受决定；
3. 每层静态热点名单按校准集**命中次数 top-n** 选取（仅校准集，
   策略选择集/最终验收集不参与）；
4. GEMM 冻结至本目标完成（moe_gemm.cu / moe_decode.cu 等不改；
   驻留正确性修复范围限定驻留层）。

## 3. 身份（矩阵执行时核对）

| 项 | 值 |
|---|---|
| 分支/commit | codex/moe-residency-20260930 @ f976eb3（+18:06 统计日志构建） |
| 二进制 | build/q4t（sha256 见 e2e-*/binary.sha256） |
| 模型 | ~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream（只读） |
| 夹具 | e2e-fixtures-v2（sha 已核，基线/候选同一份） |
| 服务条件 | --max-seq 1 --max-prefill 8192 --max-len 262144 --max-tokens 256 --no-mtp |
| 基线 | --moe-resident-slots 0（缺省，全常驻路径逐位不变） |
| 候选 | --moe-resident-slots 256 --moe-hot-list hot-lists/hot-256.json |
| 热点名单 | tools/trace/make_hot_list.py，校准集命中次数 top-256/层 |
| 档位 | 1024/4096/8192/45056/204800 + 目标档 261887 输入/257 输出，各 3 请求 |
| 超时 | evalscope read 7200s / total 10800s（冻结） |

## 4. 内存账与峰值（实测口径：monitor_memory.py，RSS+GPU）

- 预算 54,000,000,000 B；C=256 路由专家驻留 33.98 GB，超支已获
  用户明确接受（决策 2）。
- 已测峰值（exp12/05:01 口径）：基线 C=0 = 91.33 GB；候选
  C=256 = 57.71 GB（超 54 GB 门槛 3.7 GB，标注用户接受）。
- 全矩阵内存峰值：待填（e2e-*/memory/memory-peak.json，
  service_total_physical_peak_bytes；swap 分列记录）。

## 5. 正确性合同（已测证据）

- C=256+hot-256 1024 档输出与 C=0 逐位一致（exp12，15:58 构建）；
- GEMM 冻结重建后 C=0 1024 档与旧基线逐位一致（verify-gemmfreeze）；
- 全矩阵逐档输出 sha256 基线 vs 候选逐位核对：待填（compare-report）。

## 6. E2E 矩阵结果（待填）

冻结口径：decode=(out−1)/(latency−ttft)，逐请求计算后取调和均值；
门槛=候选/基线 ≥ 50% 逐档；首请求与后续请求分列；TTFT/总耗时取
算术均值；加载量取 server.log [residency] 行（loads/load_mb/misses，
decode/prefill 分列）；目标档计数合同 in=261887 out=257
finish=length。

| 档 | 基线 decode | 候选 decode | 比值 | 50%门槛 | TTFT 基/候 | 耗时 基/候 | 逐位一致 |
|---|---|---|---|---|---|---|---|
| 1024 | 待填 | 待填 | | | | | |
| 4096 | 待填 | 待填 | | | | | |
| 8192 | 待填 | 待填 | | | | | |
| 45056 | 待填 | 待填 | | | | | |
| 204800 | 待填 | 待填 | | | | | |
| 261887(总262144) | 待填 | 待填 | | | | | |

已知风险（矩阵前冻结分析）：C=256 首请求冷加载代价大（45K 档
1135.9s vs 基线 ~45s，loads=1.13M/3.1TB），prefill 加载主导 TTFT；
门槛只作用于 decode 吞吐。目标档首请求总加载外推 ~18TB，页缓存
预热（45K/200K 档先行）可显著降低；若逼近 7200s 超时按失败处理，
不放宽。

## 7. 受影响检查（矩阵后执行）

- 固定质量题：候选 vs 基线同题输出对照（复用既有 11 题集）；
- 业务输出对照：复用已授权业务材料短请求，候选 vs 基线；
- 补载失败注入：NVMe 读失败路径的服务语义（500/503、槽位释放、
  不健康标记）；
- 取消：prefill 补载中取消、decode 补载中取消的槽位与状态回收；
- 槽位复用安全：连续请求 LRU 驱逐/复用后输出逐位一致（矩阵 3 请求
  序列已部分覆盖，另做定向短序列）；
- 跨请求状态：LRU/映射跨请求保留不污染后续请求（矩阵逐档逐位核对
  覆盖）。

## 8. 回退

- `--moe-resident-slots 0`（默认）= 原全常驻路径逐位不变；
- 工作分支 codex/moe-residency-20260930 阶段 commit 可回退；
  不推 main 直到验收；默认部署（3b414633）不受影响。

## 9. 复现

```bash
# 基线矩阵
bash .q4t-work/moe-residency-20260930/run-e2e.sh baseline-s0-current
# 候选矩阵
bash .q4t-work/moe-residency-20260930/run-e2e.sh cand-c256 \
  --moe-resident-slots 256 \
  --moe-hot-list .q4t-work/moe-residency-20260930/hot-lists/hot-256.json
# 冻结口径对比
python3 .q4t-work/moe-residency-20260930/compare_e2e.py
```

证据根目录：.q4t-work/moe-residency-20260930/（e2e-baseline-s0-current/、
e2e-cand-c256/、compare-report.txt、hot-lists/、bitexact/）。
