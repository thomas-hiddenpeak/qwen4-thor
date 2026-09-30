# MoE 分层专家驻留 — 部署与回退说明（2026-09-30）

状态：DRAFT。矩阵与受影响检查完成后按实测结果定稿。

## 1. 配置

| 项 | 基线（默认） | 候选 |
|---|---|---|
| 服务选项 | （缺省） | `--moe-resident-slots 256 --moe-hot-list <hot-256.json>` |
| 路由专家驻留 | 48×512 全常驻（67.95 GB） | 48×256 槽（33.98 GB）+ 按需补载 |
| 数值语义 | 完整 Router/top-k + 全部专家 | 相同（补载专家与常驻专家逐位一致） |
| 内存峰值（exp12 口径） | 91.33 GB | 57.71 GB（超 54 GB 门槛，用户已接受） |

热点名单 `hot-256.json`：每层校准集命中次数 top-256，
`tools/trace/make_hot_list.py` 生成（仅校准集；策略选择集/最终验收集
不参与）。名单文件随部署包分发，路径在启动命令中显式给出。

## 2. 部署（候选）

```bash
./build/q4t serve \
  --model-dir ~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream \
  --port 8000 --max-seq 1 --max-prefill 8192 --max-len 262144 \
  --max-tokens 256 --no-mtp \
  --moe-resident-slots 256 \
  --moe-hot-list .q4t-work/moe-residency-20260930/hot-lists/hot-256.json
```

启动后自检：
- `/health` 200；
- 1024 档短请求输出与基线逐位一致（部署后必做，命令见 §4）；
- `server.log` 无 `[residency]` 错误行；`[q4t][residency]` 请求行
  loads/misses 计数符合预期（首请求冷加载高，后续请求低）。

## 3. 回退

1. **运行级回退（秒级）**：去掉两个驻留选项重启，即回到全常驻路径
   （`--moe-resident-slots 0` 缺省），代码路径逐位不变。
2. **版本级回退**：工作分支 `codex/moe-residency-20260930` 的阶段
   commit 可回退；默认部署（`text-v1-moe-trace-20260928` /
   `3b414633`）不受影响，可用 `rollback-default.py` 恢复。
3. 回退后验证：1024 档输出与回退前基线逐位一致。

## 4. 部署后验证命令

```bash
# 1024 档逐位对照（基线 vs 候选，同一二进制）
python3 .q4t-work/moe-residency-20260930/affected/affected_http.py \
  --requests <1024请求.jsonl> --out /tmp/dep-base --port 8001
# （候选服务同法再跑一次，对比 results.json 的 text 字段）
```

## 5. 已知边界

- 首请求冷加载代价大（NVMe 2.7 GB/s；45K 档 exp12 实测 1135.9s）；
  页缓存预热后显著降低。请求超时冻结为 read 7200s / total 10800s。
- 补载失败语义：请求 500（prefill/decode 阶段分别报
  "prefill failed"/"generation failed"），服务不标记 GPU 不健康，
  槽位簿记保持一致（加载完成才标记驻留）；一次性故障注入检查见
  验收报告 §7。
- GEMM 冻结至本目标完成（用户 2026-09-30 指示）。
