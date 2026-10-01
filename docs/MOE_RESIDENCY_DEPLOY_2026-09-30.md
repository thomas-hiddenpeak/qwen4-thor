# MoE 分层专家驻留 — 部署与回退说明（2026-09-30）

状态：DRAFT。矩阵与受影响检查完成后按实测结果定稿。

## 1. 配置

| 项 | 基线（默认） | 候选 |
|---|---|---|
| 服务选项 | （缺省） | `--moe-resident-slots 256 --moe-hot-list <hot-256.json>` |
| 路由专家驻留 | 48×512 全常驻（67.95 GB） | 48×256 槽（33.98 GB）+ 按需补载 |
| 数值语义 | 完整 Router/top-k + 全部专家 | 相同（补载专家与常驻专家逐位一致） |
| L2 CPU 专家缓存 | 无 | 每层 Q4T_MOE_L2_SLOTS（默认 128，钳制 [8,512]）pinned LRU 池，48×128×3,276,816 B = 20.13 GB，启动即全额分配 |
| 内存峰值 | 91.33 GB（全矩阵实测） | 第一轮 57.71 GB（超 54 GB 门槛，用户已接受）；第二轮（+L2-128）实测待填；第三轮 nu-15552+L2-8 估算 ≈68.8 GB（用户已接受超支，2026-10-01 07:25） |

热点名单 `hot-256.json`：每层校准集命中次数 top-256，
`tools/trace/make_hot_list.py` 生成（仅校准集；策略选择集/最终验收集
不参与）。名单文件随部署包分发，路径在启动命令中显式给出。

**按层非均匀 C（第三轮二进制，62edd6a 起）**：`--moe-resident-slots`
为**每层上限 cap**；运行时按热点表逐层取
`C_l = min(hot_list[l].长度, cap)`，启动日志打印逐层 C。均匀表
（各层等长）时 `C_l == C` 全层，行为与旧版完全一致（bitexact-c256
回归门覆盖）。nu-15552 部署：`--moe-resident-slots 446`（= 最大
C_l）+ `hot-nu-15552.json`（48 层、总 15552、C_l 256..446，
`tools/trace/make_hot_list_nu.py` 按同一冻结校准切分命中次数 top-n
DP 最优分配）+ `Q4T_MOE_L2_SLOTS=8`。内存预算估算已按层实际驻留数
计费（chat_server.cpp，62edd6a；旧式在 cap=446 下会少计 ≈16.2 GB）。

## 2. 部署（候选）

```bash
./build/q4t serve \
  --model-dir ~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream \
  --port 8000 --max-seq 1 --max-prefill 8192 --max-len 262144 \
  --max-tokens 256 --no-mtp \
  --moe-resident-slots 256 \
  --moe-hot-list .q4t-work/moe-residency-20260930/hot-lists/hot-256.json
```

第二轮二进制（L2+常驻加载线程池）无需新增启动参数：L2 池在
`--moe-resident-slots > 0` 时随服务启动分配；容量用环境变量
`Q4T_MOE_L2_SLOTS`（默认 128；64/32 可复测降内存，无需改码）。

第三轮（按层非均匀，用户已授权）：

```bash
Q4T_MOE_L2_SLOTS=8 ./build/q4t serve \
  --model-dir ~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream \
  --port 8000 --max-seq 1 --max-prefill 8192 --max-len 262144 \
  --max-tokens 256 --no-mtp \
  --moe-resident-slots 446 \
  --moe-hot-list .q4t-work/moe-residency-20260930/hot-lists/hot-nu-15552.json
```

启动后自检：
- `/health` 200；
- 1024 档短请求输出与基线逐位一致（部署后必做，命令见 §4）；
- `server.log` 无 `[residency]` 错误行；`[q4t][residency]` 请求行
  loads/misses 计数符合预期（首请求冷加载高，后续请求低）。

## 3. 回退

1. **运行级回退（秒级）**：去掉两个驻留选项重启，即回到全常驻路径
   （`--moe-resident-slots 0` 缺省），代码路径逐位不变；按层非均匀
   配置同样适用（换回 hot-256.json + cap 256 即回到均匀 C=256）。
2. **L2 容量回退**：`Q4T_MOE_L2_SLOTS=64`（+10.07 GB）或
   `=32`（+5.03 GB）重启即可降低 pinned 内存，无需改码；L2 不能
   完全关闭（下限为加载线程数 8）。
3. **版本级回退**：第一轮二进制封存于
   `.q4t-work/moe-residency-20260930/bin-2346-fixed/`（sha256
   4c5cddea…，无 L2）；工作分支 `codex/moe-residency-20260930` 的
   阶段 commit 可回退；默认部署（`text-v1-moe-trace-20260928` /
   `3b414633`）不受影响，可用 `rollback-default.py` 恢复。
4. 回退后验证：1024 档输出与回退前基线逐位一致。

## 4. 部署后验证命令

```bash
# 1024 档逐位对照（基线 vs 候选，同一二进制）
python3 .q4t-work/moe-residency-20260930/affected/affected_http.py \
  --requests <1024请求.jsonl> --out /tmp/dep-base --port 8001
# （候选服务同法再跑一次，对比 results.json 的 text 字段）
```

## 5. 已知边界

- 冷加载代价大：第一轮全矩阵实测 NVMe 有效吞吐 4.6–5.5 GB/s
  （45K 档 1.16 TB/249s；200K 档 6.17 TB/1240s；目标档 7.99 TB/28min）。
  页缓存预热**未**降低加载量（工作集远超 RAM，TTFT 三请求持平）；
  第二轮 L2 命中后稳态补载为 H2D-from-RAM，实测待填。请求超时冻结为
  read 7200s / total 10800s。
- 补载失败语义：请求 500（prefill/decode 阶段分别报
  "prefill failed"/"generation failed"），服务不标记 GPU 不健康，
  槽位簿记保持一致（加载完成才标记驻留）；一次性故障注入检查见
  验收报告 §7。
- GEMM 冻结至本目标完成（用户 2026-09-30 指示）。

## 6. 最终验收执行规程（2026-10-01，r3 矩阵后）

前提：r3 自动链（auto-r3c → compare → post-r3-affected →
auto-c1-verify/verify-c1）全部完成，verify-c1 六查通过，
section5-branch.txt 已写出。

1. **合并 C1+C2 二进制**：path-c 分支（wt-c1 @ df02f60）并入工作
   分支主 HEAD（merge-tree 核验 0 冲突；7e3cadc 之后主分支仅
   docs/scripts 改动，src/ 零差异），重建 build/q4t，记录 sha256。
   合并前 verify-c1 已用同一 commit 的 worktree 二进制（ba7327a9）
   完成六查，合并后以 1024 档 bitexact 复验。
2. **冻结 B**：按 R3_DECISION 末节选择算法，B ∈ {7680,8448,9216,
   9984}（§5 分支 1/2 且 C≥224 时追加 10752），取同时满足
   内存（rss+gpu 峰值+模型页缓存增量 ≤54 GB）、性能（六档 decode
   调和均值 ≥50% 基线）、正确性（bit-exact+质量+业务+受影响检查）
   的最大 B；估算峰值 49.2/51.1/53.1/55.1 GB（ledger §1.6），
   B=9984 页缓存预算为负，优先从 9216 起验。
3. **最终验收**（C3 页缓存协议 + 全矩阵，基线与候选同节奏）：

```bash
bash .q4t-work/moe-residency-20260930/final-acceptance.sh <B>
# 内部：c3-pagecache-protocol.sh acc-base-c0 (C=0) --acceptance
#       c3-pagecache-protocol.sh acc-final-<B> (cap/hot-final-<B>, L2-16)
#       compare_e2e.py acc-base-c0 acc-final-<B>
#       内存门：rss+gpu 峰值 + 模型页缓存增量 ≤ 54,000,000,000 B
```

4. **正确性收尾**：bitexact-final.sh（C=0 vs 候选，1024/8192）+
   质量 11 题 + 业务 6 请求 + 受影响检查（补载失败/取消/槽位复用/
   跨请求状态，post-r3-affected 已覆盖质量/业务，故障/取消在
   verify-c1 [4/6][5/6]）。
5. **定稿**：验收报告（MOE_RESIDENCY_ACCEPTANCE_2026-09-30.md）
   与本文按实测回填；同一候选配置同时满足容量/内存/性能/正确性
   才写"通过"；否则按失败维度报告差距，不放宽目标。

回退（最终候选失败时）：按 §3 运行级回退到 `--moe-resident-slots 0`；
版本级回退到合并前 commit（合并前 HEAD 已记录于本节执行日志）。
