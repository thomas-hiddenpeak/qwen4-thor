# MoE 分层专家驻留与按需加载 — 实现计划 (2026-09-30)

目标（来自 /goal，2026-09-30 用户澄清）：单 Thor、单流、max_seq=1、文本
greedy、MTP 关、保持精度与计算合同；容量 262144；服务+辅助进程物理内存峰值
≤ 54,000,000,000 B；五档+目标长度档 decode 吞吐 ≥ 同条件全专家常驻基线的
50%；通过数值合同/HTTP/生命周期验收，保留可回退版本。

**目标长度档定义（2026-09-30 用户澄清，替代原"261888 输入"口径）**：
以**总上下文 = 输入 + 输出 = 262144** 为准，不是"输入 256k"。实现取
输入 261887 + 输出 257（max_tokens=257）。261888 输入在冻结夹具上触发
立即 EOS（boundary4 证据），用户已明确指示不再追究 261887/261888 的
一字之差，该边界调查关闭；验收只看总上下文达到 262144 且服务端 token
计数完整、无截断。

## 内存账（262144, max_seq=1, MTP 关）— 待实测复核

| 项 | 字节 | 说明 |
|---|---:|---|
| 非路由权重 | ~16.0e9 | embed/lm_head/attn/linear/HC/PLE/共享专家（84.00 GB 总权重 − 67.95 GB 路由） |
| 路由专家常驻 C 槽/层 | C×48×2,765,056 | C=32→4.27 GB, C=64→8.52 GB, C=96→12.78 GB |
| 主 workspace | 2.68e9 | kMainWorkspaceBytes (@8192) |
| forward 缓冲 | ~0.4e9 | ForwardBufferBytes |
| MTP workspace | 5.0e9 | MTP 关时**不应分配**（待核实并条件化） |
| PLE working | 0.075e9 | staging+gpu_fp8+ring |
| context margin | 2.0e9 | CUDA context + 系统余量 |
| state pool (262144) | 8.86e9 | KV/indexer/rope/SSM/PLE-conv/调度 |
| per-request trunk+draft | 9.44e9 | trunk(262144×hc×2) + draft_logits(8192×vocab×2)；MTP 关时 draft 不应分配 |
| staging 10 槽 | 27.65e6 | 跨层补载暂存 |

C=64 且 MTP/draft 条件化后估算 ≈ 16+8.52+2.68+0.4+0.075+2+8.86+5.37+0.03 ≈ 44 GB，
留 ~10 GB 余量给 CUDA context/driver/页缓存。C=96 ≈ 48 GB。**C 的最终值以实测
内存账 + 容量—缺失曲线决定**，不预设。

## 关键数值事实
- 每专家 payload 2,764,816 B，256 对齐槽 2,765,056 B（gu 1,638,400 + dn 819,200
  + gu_sf 204,800 + dn_sf 102,400 + 4×f32 16）。
- 全路由 payload 48×512×2,765,056 = 67,954,016,256 B。
- NVMe direct 实测 2.7 GB/s（page cache 命中时更高，但 68 GB 工作集 > 可用缓存）。
- 回放（64 槽 LRU continuous，外部5条）decode 选择命中 69.18%，逻辑加载
  130.9 GB / (5×64 decode×48 层) ≈ 8.5 MB/层·token → 串行 ~1.7 ms/层 → ~81 ms/token。
  **串行补载在 64 槽下可能低于 50% 门槛**，需靠：更大 C、与计算重叠、页缓存命中。
  这是本目标的核心风险，必须实测，不假设可达。

## 实现合同（保持计算语义）
1. 保留完整 Router/top-k 与专家计算；不减少专家、不降精度。
2. 专家身份→槽位映射；缺失补载；**加载完成后才使用**；最后 GPU 使用完成后
   才安全复用槽位（stream-ordered）。
3. 补载不改变求和顺序/数值：同一专家同一权重，驻留或补载结果逐位一致。
   **正确性判据：驻留模式与全常驻模式同输入输出逐位一致**（同一批专家、同一权重）。
4. prefill 按当前分块计算分组执行；decode 走 MoEDeviceDecode 分组 GEMM。

## 本轮范围（冻结）
- 新增 `quant::MoEResidency`：每层 C 槽池 + expert→slot 映射 + LRU + 单专家加载器
  （复用 LoadMoEWeights 的逐专家读/swizzle/H2D 逻辑）。
- `MoEWeightLayout` 增加槽位模式（C=E 为全常驻恒等映射，保持现有路径逐位不变）。
- `MoERoutedForward`（prefill）与 `MoEDeviceDecode`（decode）支持槽位指针。
- 服务选项 `--moe-resident-slots C`（0/缺省=全常驻基线；C>0=C 槽/层）。
- 静态热点初装：用校准集逐层 top-C 名单填充初始槽（名单来自业务集校准，非留出）。
- 单测：单专家加载与全常驻逐位一致；驻留模式短请求输出与全常驻逐位一致。
- 出口：零警告构建；全常驻模式输出与基线逐位一致；驻留模式短请求逐位一致；
  驻留模式 GPU 显存下降（C 槽 vs 512 专家）。

## 不在本轮
- 预取/预测（先做串行补载，实测吞吐后决定是否加）。
- 非均匀按层分配（先均匀 C，容量—缺失曲线出来后评估）。
- 真实 offload 性能验收（按 EVALUATION.md，首项测试 HTTP E2E，下一轮）。


## 用户决策记录（2026-09-30 下午，按时间顺序）

1. **目标长度档口径**：以总上下文 = 输入 + 输出 = 262144 为准，不是
   "输入 256k"。261887/261888 的一字之差不再追究（boundary4/5 证据保留，
   边界调查关闭）；验收只看总上下文达到 262144 且服务端 token 计数完整、
   无截断。实现取输入 261887 + 输出 257（max_tokens=257）。
2. **C=256 候选**：用户明确支持做 C=256（每层 256 槽），即使内存预算
   超支也接受（C=256 路由专家驻留 33.98 GB，整机物理内存峰值预计
   ~72-80 GB，超过 54 GB 门槛——该超支已获用户明确接受，验收报告
   如实报告实测峰值并标注此接受决定）。C=64 仍同步推进。
3. **静态热点名单口径**：每层专家按校准集命中次数（token 加权选择
   频次）top-n 选取，仅用校准集（policy/验收集不参与）；
   tools/trace/make_hot_list.py 生成，hot-64/96/128/256.json 已封存于
   .q4t-work/moe-residency-20260930/hot-lists/（hot-64 重生成逐字节
   一致，命令可复现）。
4. **GEMM 冻结**：用户指示当前目标完成前不修改 GEMM（moe_gemm.cu /
   moe_decode.cu 等）；驻留正确性问题的修复范围限定在驻留层
   （MoEResidency / MoEForward 槽位重映射 / 加载路径），目标完成后再
   讨论 GEMM 改动。

## 回退
- `--moe-resident-slots 0`（默认）= 现有全常驻路径，逐位不变，可随时回退。
- 阶段 commit 到工作分支，不推 main 直到验收。

## 冻结轮（2026-09-30 晚）：全矩阵 E2E 对比

### 已有证据（exp12，16:42，15:58 构建）
- s0（C=0）：1024 档 wall 14.7s；目标档 in=261887 out=257 total=262144
  wall 227.9s（总上下文达标，服务端计数完整）。
- C=256+hot-256：1024 档 BIT-EXACT vs s0 = True（正确性合同通过）；
  1024 档 wall 99.6s（首请求冷，loads=33757/93.3GB）；45056 档
  wall 1135.9s（loads=1126948/3.1TB，misses=1126948）。
- 内存峰值（monitor_memory.py 口径 RSS+GPU）：s0=91.33 GB，
  c256=57.71 GB（超 54 GB 门槛 3.7 GB，用户已明确接受，见决策记录 2）。
- 05:01 旧基线（同 45056 prompt，sha 核对一致）参考值：1K TPOT 53.85ms、
  44K TPOT 55.86ms、200K TPOT 58.32ms；261888 输入档立即 EOS（3 token），
  已由 261887+257 总上下文口径替代。

### 50% 门槛口径分析（冻结）
- 门槛作用于 **decode 吞吐** = (out−1)/(latency−ttft)（EVALUATION.md
  Decode 定义），prefill 加载时间只影响 TTFT（报告项，非门槛项）。
- decode 每 token 缺失负载（C=256 实测 miss 率外推）≈ 20–30 次/层组
  ×2.765MB ≈ 55–85MB/token → 2.7GB/s NVMe 下 ≈ 20–32ms/token，
  叠加基线 TPOT 54–58ms 后预计 decode 仍 ≥ 60–70% 基线；
  页缓存命中后更低。prefill 加载使 TTFT 大幅上升（45K 档预计 ~18min），
  请求超时冻结为 evalscope read 7200s / total 10800s（沿用现有配置）。
- 风险：目标档首请求总加载 ≈ 18TB（外推），若 miss 率高于 45K 档外推
  值可能逼近 7200s 超时；页缓存预热（矩阵内 45K/200K 档先行）可显著
  降低。实测为准，不预设通过。

### 本轮假设、范围与出口
- 假设：C=256+hot-256（top-n 命中次数初装，无 hot-protect，LRU 动态）
  在全部六档 decode 吞吐 ≥ 基线 50%，逐档输出与基线逐位一致，
  目标档总上下文 262144 且计数完整；内存峰值如实报告（超支已获接受）。
- 范围：**无代码改动**（GEMM 冻结；驻留层冻结在 exp12 状态；
  18:06 构建仅新增分阶段驻留统计日志）。只执行：基线矩阵（C=0）→
  候选矩阵（C=256+hot-256）→ 冻结口径对比（compare_e2e.py）。
  同一二进制、同一夹具（e2e-fixtures-v2，sha 已核）、同一条件
  （max_seq=1、greedy、MTP 关、max-len 262144、每档 3 次）。
- 出口：两矩阵完整（各 6 档×3 请求，无超时/失败），对比报告
  compare-report.txt 生成；decode 门槛逐档判定；正确性逐档核对；
  内存峰值记录。任一档 decode < 50% → 下一轮评估 CPU 二级缓存
  （内存包络内）或更大 C/预取，不放宽门槛。
- 产物：queue-baseline-c256.sh、compare_e2e.py、e2e-baseline-s0-current/、
  e2e-cand-c256/、compare-report.txt（均 .q4t-work/moe-residency-20260930/）。

## 冻结轮执行记录（2026-09-30 晚，追加）

1. 基线矩阵（C=0，18:22–18:49，18:06 构建）：六档×3 全部通过；
   目标档 3/3 in=261887 out=257 finish=length；decode 调和均值
   1024/4096/8192/45056/204800/261887 = 18.569/17.861/18.140/
   17.780/17.134/16.848 tok/s；TTFT 均值 0.80/2.65/5.18/30.56/
   159.64/213.01 s；内存峰值 91,345,694,720 B。
2. 候选矩阵（C=256+hot-256）启动即段错误（rc=139）。gdb 定位：
   StageExpert 的 memcpy 写穿 pinned staging——18:06 构建将 SF 区
   放到 w 对齐偏移（s_ga=4w、s_up=5w、gu_s_merged=6w），布局总长
   6w+2s+块+16，超出 MoEResidencyStagingBytes 分配 3w+5s+块+16，
   每专家越界 3*(w−s)=2,150,400 B（w=819,200、s=102,400）。
   exp12（15:58 构建）未含该布局改动，故当时通过。
3. 修复（驻留层，GEMM 未动）：StageExpert/CommitExpert 的 SF 偏移
   改回打包（s_dn=3w、s_ga=+s、s_up=+s、gu_s_merged=+s；
   CommitExpert gu_sw=3w+5s），两函数各加"布局超出缓冲"快速失败。
   零警告重建。
4. 复验与重跑：bitexact-c256（C=256 vs C=0，1024/8192 逐位）通过后，
   同一修复二进制重跑基线+候选全矩阵（保持同一二进制口径）；
   18:22 基线结果保留为 C=0 路径跨构建稳定性证据（C=0 代码路径
   未受本次改动影响）。

## 第二轮（2026-10-01）：L2 + 常驻线程池，冻结

### 触发
冻结轮候选矩阵（23:46 二进制）四档完成即确认 decode 22–27% 基线
（1024/4096/8192/45056，各 3/3 逐位一致），全部低于 50% 门槛；根因
为逐步、逐层串行 NVMe 补载（每步约 25 miss × 约 6–7 ms，48 层顺序
执行，8 线程并行度用不上）。按冻结出口进入下一轮：CPU 二级缓存
（内存包络内）与/或预取，不放宽门槛。

### 假设
L2（128 pinned 缓冲/层，LRU，命中免 NVMe 仅 H2D）+ 常驻加载线程池
（消除每补载块 8 线程创建/回收，实测 0.173 ms/轮）使六档 decode
调和均值 ≥ 基线 50%；输出与基线逐位一致；目标档总上下文 262144、
计数完整；内存峰值如实报告（C=256 超支 57.71 GB 已获接受，L2 默认
128/层约 +17 GB 固定项，预算核算后 max_len=262144 不被 cap）。

### 范围（冻结）
仅驻留层/服务层/测试，GEMM 冻结未动（用户 2026-10-01 再确认）：
1. L2 pinned CPU 专家缓存：Q4T_MOE_L2_SLOTS（默认 128，
   [load_threads,512]）；命中快路径免 NVMe 读；victim 跳过
   in-flight/claimed；全忙同步等待 LRU in-flight H2D；统计
   l2h/l2m/l2ev/nvme_mb（commit 35b9f7d）。
2. 一次性补载故障钩子 Q4T_RESIDENCY_FAIL_EXPERT（仅测试默认关，
   affected-fault 两步合同所需，同 commit）。
3. LoadPhase1 常驻线程池：Init 创建 load_threads_ 个持久 worker
   （条件变量派发/完成回收），Free 停止并 join（commit 见当日提交）。
4. 验证：零警告构建；q4t_tests（106 项，含
   residency_l2_cache_hit_evict）；bitexact-c256（1024/8192 逐位）；
   affected 四查（补载失败/取消/槽位复用/业务对照）。
5. 第二轮全矩阵：基线 C=0 与候选 C=256+hot-256，同一新二进制、
   同一夹具（e2e-fixtures-v2）、六档×3、--request-deadline-ms
   10800000、内存监控；compare_e2e.py 冻结口径对比。

### 出口
- 全档 decode ≥ 50% 且逐位一致 → 进入完整验收（数值合同/HTTP/
  生命周期/质量对照），填验收报告。
- 任一档 < 50% → 第三轮（不放宽门槛）：逐 miss 时延分解
  （StageExpert 内 NVMe pread/swizzle/H2D 计时）、L2 容量调参
  （256/层约 +34 GB，需确认）、C=324（路由驻留 43 GB，整机约
  66 GB，需用户再次确认超支）、decode 预测性预取（lag-1 路由
  重合仅随机基线 2.3 倍，收益有限，最后手段）。
- 超时或输出不足不得记为通过；首请求与后续请求分列报告。

### 风险
- 首请求（冷 L2）decode 仍走 NVMe：估算三请求调和均值约 48–66%
  基线（取决于逐 miss 实测时延），可能贴近门槛；实测为准。
- 磁盘余 2.4 GB：矩阵产物预计 <1 GB，观察；必要时先封存旧产物
  再清理（不删证据）。
