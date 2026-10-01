# 补载管线优化（路径 C）设计 — 基于 r3 逐 miss 计时数据（2026-10-01）

状态：设计冻结（待 r3 矩阵结果校准 C 侧结论）。GEMM 冻结（仅驻留层
改动）。二进制 efce31d8。

## 1. 实测数据（timing-collect-r3，efce31d8，C=256+hot-256+L2-8，
单条 45056 输入 + 256 输出，Q4T_RESIDENCY_TIMING=1）

| 指标 | 值 | 说明 |
|---|---:|---|
| pread_avg | 2.40 ms/专家 | NVMe 读（2 大 pread + 1 小），页缓存仅 3.7 GB（冷读主导） |
| swz_avg | 0.23 ms/专家 | SF swizzle（CPU） |
| stage_avg | 2.89 ms/专家 | pread + swz + 锁/事件开销 |
| phase1_avg | 3.59 ms/chunk | 8 worker 并行 stage 一个 chunk（≈单读时间，NVMe 并行有效） |
| d2h_avg | 3.12 ms/层调用 | D2H+cudaStreamSynchronize（GPU 追赶，含流上 H2D） |
| loads / nvme | 430524 / 1.19 TB | 重读因子 17.5×（24576 唯一专家） |
| L2-8 命中 | 0（l2h=0） | 8 缓冲太小，全部走 miss 路径 |
| decode miss | 6585/256 token = 25.7/token | = 0.54/层/步（48 层） |
| prefill miss | 411651 | TTFT 288 s（基线 45056 TTFT 30.55 s） |
| r2 同档对照 | 基线 dec 17.84 tps / 候选(L2-128) 4.52 tps (25.4%) | 50% 门槛 = ≥8.9 tps（≤112 ms/token） |

## 2. decode 临界路径模型（每 token，C=256+L2-8）

- 无 miss 层：d2h+GEMM 发射 ≈ 1.1 ms × 48 ≈ 53 ms
- 有 miss 层：+ phase1 3.6 ms + phase2 ≈0.2 ms；0.54 miss/层 →
  ≈ 95 ms
- 流上 H2D（2.76 MB/miss，Thor H2D 估 4–10 GB/s）→ d2h 等待 +10–20 ms
- 合计 ≈ 160–170 ms/token（实测 r2 同档 206–221 ms，差值来自
  L2-128 开销与 d2h 均值偏高）

**结论：decode 瓶颈 = 每 miss 的 NVMe stage 在 CPU 临界路径上
（2.4–3.6 ms），不是 H2D、不是 swizzle、不是 GEMM。**

## 3. 关键否定结论（分析推演 + r2 L2 数据佐证）

**「预取上一步路由」对 decode 无效**：t+1 步的 miss 专家 e 必然
不在 t 步的 needed 集合中——若 e 在 t 步被需要，LRU 保护
（needed_mark）使其不被驱逐，t+1 步仍是 slot 命中。decode miss
是「久未使用、重新回来」的专家，上一步集合无法预测。r2 L2-128
decode 侧 l2h 极低（总 l2h=760/45056 档，prefill 主导）与此一致。
→ 不做该预取；L2 只保留为补载管线缓冲 + 跨 sub-chunk 缓存。

## 4. 候选优化（按预期收益排序，均仅驻留层）

### C1 流水线化 stage→commit + worker 8→16（低风险，先做）
- 现状：LoadPhase1 整 chunk stage 完（CPU 阻塞 ~3.6 ms）→
  LoadPhase2 串行 commit（9 次 H2D/专家）。NVMe 与 H2D 零重叠。
- 改法：worker stage 完立即 CommitExpert（H2D 流序，安全：GEMM
  在同一 stream 上 H2D 之后发射）；chunk 屏障只保留错误聚合。
  kMaxLoadThreads 8→16（L2 下限随 worker 数）。
- 预期：多 miss chunk（prefill sub-chunk ~34 miss）省 ~30%
  backfill；单 miss decode 层收益小（stage 仍是临界）。
- 出口：bitexact 回归 + 45056 单条 TTFT 对比。

### C2 H2D 合并（低风险，随 C1）
- ga+up 权重在 L2 缓冲与 GPU 槽位内均连续 → 2×w_bytes 一次拷贝；
  9 次 H2D/专家 → 7 次。省 ~20–30 µs/专家（次要）。

### C3 页缓存策略（内存侧，决定 miss 单价）
- miss 单价 = max(页缓存命中 ~0.2–0.3 ms, NVMe 冷读 2.4 ms)。
  最终候选 rss+gpu 越小，内核可给模型页缓存越多（整机 131.92 GB）。
- 预算口径：目标 54 GB **含模型相关页缓存**（目标原文）。最终候选
  需 rss+gpu + 模型页缓存 ≤ 54 GB：
  - C=224：rss+gpu ≈ 54.2 GB → 页缓存 ≈ 0（不可行）
  - C=208：≈ 51.7 GB → 页缓存 ≈ 2 GB（部分热专家）
  - C=160：≈ 44.5 GB → 页缓存 ≈ 9 GB（~18% 模型常驻页缓存）
  - C=128：≈ 39.8 GB → 页缓存 ≈ 14 GB
  （C 每 −32 层席位 ≈ 省 4.3 GB，按 2,764,816 B/专家×48 层）
- 页缓存命中使 miss 单价 2.4 ms → ~0.25 ms：decode ≈
  53 + 48×0.54×0.25 ≈ 60 ms/token → ~95% 基线（远超 50% 门槛）。
- 风险：页缓存受内核回收策略影响，需固定 drop_caches 口径 +
  预热协议（验收前对模型文件做受控 readahead，记录命中证据）。

### C4 驱逐镜像（write-back，中风险，视 r3 结果决定）
- slot 驱逐前把被驱逐专家 payload D2H 进 L2（2.76 MB，~0.3–0.7 ms
  流上），使「近期驱逐、重新回来」的 miss 变 L2 命中。
- 仅镜像最近 K 次驱逐（L2 容量约束）；K=32/层 ≈ 覆盖 decode
  ~59 步内的回归专家。成本 = 每驱逐一次 D2H（418k/请求 × 2.76 MB
  全量不可行，必须限 K）。
- 出口：decode miss→L2 命中率实测；不达标即弃。

### C5 C 席位（r3 矩阵直接测量中）
- nu-15552（按层 top-n，C_l 256..446）miss 数按席位比例下降；
  若其 decode ≥50% 基线 → 最终候选 = 54 GB 内最大 C（≈208–224）
  + C1/C2/C3，按 r3 数据外推并用一次定向矩阵验证。

## 5. 决策逻辑（r3 矩阵出报告后）

1. nu15552 各档 decode ≥50% 且 c256 <50% → 席位是主杠杆：
   最终候选 C≈208–224 + C1+C2+C3，定向验证（六档×3）。
2. c256 本身 ≥50%（C1/C2 生效后复测）→ 最终候选 C=256 需再省
   4.5 GB（MTP workspace 条件化等）或降 C=224，定向验证。
3. 两者都 <50% → 上 C4（驱逐镜像）+ C3 页缓存预热组合，复测；
   仍不达标则报告差距与所需决策（不放宽目标）。

## 6. 冻结范围与出口

- 改动范围：src/quant/moe_residency.{h,cpp}（+ 必要时
  include/q4t/quant/moe_residency.h）；GEMM/moe.cu 零改动
  （Resolve 调用点已存在，不新增）。
- 数值合同：任何改动后 bitexact（C=0 跨二进制 + 候选 vs 基线
  逐位一致）必须全过；L2/页缓存只改变加载路径，不改变计算。
- 出口：五档+目标档 decode ≥50% 基线 × 3 次、TTFT/加载量/内存
  峰值同报、质量×2+业务×2 受影响检查、内存账回填。

## 7. C3 运行协议（验收口径，2026-10-01 13:15 补充）

C3 页缓存收益依赖内核页缓存状态，验收必须固定口径（基线与候选
同口径，否则不公平）：

1. **冷基线**：服务启动前 `sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'`
   （本机已验证可写，2026-10-01 13:13）；记录 drop 前后
   /proc/meminfo 的 Cached/MemAvailable。
2. **受控预热**：服务启动、模型加载完成后，验收测量请求之前执行
   固定预热（45056 fixture ×1，max_tokens=8，丢弃不计入统计），
   把热工作集装入页缓存；记录预热引发的 Cached 增量（模型相关
   页缓存，单列计入 54 GB 预算口径）。
3. **证据**：验收报告记录 (a) 页缓存增量、(b) pread_avg 变化
   （冷 2.40 ms → 页缓存命中预期 ~0.2–0.3 ms，Q4T_RESIDENCY_
   TIMING=1 采集）、(c) 重读因子变化。页缓存命中不改变计算，
   仅改变加载路径，bitexact 合同不受影响。
4. **禁止**：测量期间不得手动 drop_caches 或读大文件；矩阵/验收
   在途时不得执行本协议任何步骤（会污染在途请求的页缓存状态）。
5. 预热请求数与页缓存目标大小随最终候选 C 确定（C≈160–224 对应
   9–2 GB 页缓存预算，见 §4 C3），在 r3 矩阵出报告后冻结。
