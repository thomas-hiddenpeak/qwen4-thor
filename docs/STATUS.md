# STATUS.md — 当前状态快照

> 本文始终反映"现在"。已完成/已解决归档见 [DONE.md](DONE.md);
> 开发时间线见 [log/](log/README.md)。文档总索引见 [README.md](README.md)。

## 当前阶段

Phase 2 — 连续批处理 / MTP 批处理 / 性能优化 (Phase 1 已闭合 2026-09-07)
(详见 [PHASES.md](PHASES.md))

## 当前焦点 (2026-09-19): host C++23 升级 + OOM 可靠性工程 + chunked MTP 长上下文投机解码 + serve 工业化

- **数据流驱动的逐级优化分析: 5 环节搬运账单 + L1/L2/L3 路线图 (2026-09-20,
  分析完成, 待实施)**:
  用户提出新方向: 专用模型+专用设备, 按权重与数据流向针对全部硬件
  (寄存器/缓存/SM/tensor core) 逐环节写 kernel, 分次序分级别 (L1 带宽
  无效搬运 → L2 降复杂度 → L3 融合/执行模型)。逐环节读真实源码
  (decoder_layer/hyperconnection/linear_attention/full_attention/moe/
  moe_gemm/ple_layer) 整理完整数据流 + 每环节搬运账单 + 四维度 (寄存器/
  缓存/SM/tensor core) 现状 vs 手写最优。**核心结构发现**: 窄流 [T,2560]
  计算 / 宽流 [T,10240] 残差 (4×), 宽窄桥 (HC 低秩 320) 是无效搬运结构
  性来源。**分场景决定性**: decode T=1 激活 <0.3% (纯权重带宽地板, 已
  闭合, 无优化空间); prefill T=8192 激活:权重 ≈15-20× (激活搬运主导,
  主战场)。**总账**: prefill 总激活搬运 ≈347GB/forward (HC 153.6 + GDN
  72 + MoE 94.5 + QSA 24.2 + PLE 2.62), 手写可省 ≈82.8GB (24%) — 与
  权重读取 (~84GB) 同量级。P0 = GDN (32GB, 44%, 难度最低) + HC (42.2GB,
  27%); P2 = MoE (3.8GB, GEMM 全 nvjet 已最优) + QSA (3.05GB, tensor
  core 已最优); P3 = PLE (1.7GB, 存储层级问题)。建议第一步 GDN ①+③+④
  (低风险 bit-exact 21.6GB)。**所有 GB 数字是估算, 实施前必须插桩实测**。
  详见 [DATAFLOW_OPTIMIZATION.md](DATAFLOW_OPTIMIZATION.md)。下一步: 待
  用户裁决实施顺序 (先插桩测量 / 直接 GDN ①+③+④)。
- **单流非量化 decode 瓶颈定位: 已到带宽下限, Option 1 否决 (2026-09-19, 负结果 + 方向修正)**:
  用户方向 = 单流 (B=1) 非量化 (BF16) 无 MTP decode 优化。前序日志把 "MoE
  router D2H 最大 4.48ms" 列为最大 gap 并计划 Option 1 (GPU-resident MoE
  plan)。动手前精确分解**推翻该判断**: (1) 4.48ms 窗口内 GPU 完全空闲, 唯一
  活动 = 一个 2048B D2H 占 4428µs (紧随 2052B H2D 仅 0.5µs); (2) 微基准复刻
  moe_gemm.cu:359 模式 pageable/pinned 均 21µs, **pinned 零帮助** → 非
  pageable staging; (3) **决定性**: 48 次 MoE D2H 中 47 次 2-5µs 正常, 仅 1 次
  4428µs (>1ms D2H 全 profile 仅 1 次) → **一次性异常, 非每 step**。修正后:
  MoE D2H 真实开销 ≈190µs/step (0.23%), **Option 1 否决** (多天重写省 0.23%
  不值)。真实单步 (剔除 2 个一次性异常: lm_head 5.17ms + MoE D2H 4.43ms)
  ≈73-77ms (~13.5 tok/s)。**真瓶颈 = Bf16GevKernel 33.4ms (46%)**, 实测带宽
  240-260 GB/s = **100-108% of LPDDR5x 峰值 → 已到带宽下限, kernel 无优化
  空间**。结论: 单流非量化 decode 已到内存带宽下限, 唯一再快杠杆 = 减少权重
  字节 (FP4/FP8 量化, 用户已排除) 或放宽单流 (B>1 批处理, 已闭合 B2)。
  无代码改动 (仅新增 tools/d2h_latency_bench.cu + /tmp/gev_bw.cu 微基准)。
  下一步: 待用户裁决是否放宽 "非量化" 或 "单流" 约束, 否则视为已闭合。
- **C++23 全量迁移: host + device (GCC 14 + CMake 4.4.3) (2026-09-19, 已闭合)**:
  承接上一项 (当时只升 host, device 留 C++17)。用户补充: 迁移是 NVIDIA
  工程师建议, CUDA 13.3 已全面支持 host+device C++23, "不动 device" 指可
  逐步迁移, GCC 可升级环境受控 → 改为 host+device 全 C++23。关键发现:
  (1) nvcc 13.3 有 --std=c++23 但 GCC 13 时静默忽略 (device C++23 从未
  生效); (2) GCC 14 是前提 (noble apt 无 15, 装 g++-14 14.2.0); (3) CMake
  必须 >=4.0 (CMAKE_CUDA_STANDARD 23 只在 4.x 映射到 nvcc --std=c++23,
  3.28/3.31 只到 CUDA20) → pip 装 cmake 4.4.3。实现: CMAKE_CUDA_STANDARD
  17→23 + cmake_minimum_required 4.0, 配置 -DCMAKE_CXX_COMPILER=g++-14
  -DCMAKE_CUDA_HOST_COMPILER=g++-14, 现有 .cu 代码零改动 (C++17 兼容是
  C++23 子集)。验证: .cu flags 实证含 --std=c++23, 全量重编零警告, 76
  测试绿, **E2E bit-exact** (44K 输出 == C++17 基线)。GCC 14.2 可用
  print/expected/format/ranges/jthread (device 需 relaxed-constexpr);
  mdspan/constexpr string 需 GCC 15; device 乱序指定初始化 (P1967R3) 不支持。
  下一步: 新代码采用 C++23 特性, 现有代码按需逐步迁移。
- **OOM 可靠性: 内存预算 (--mem-fraction) + auto-length + 运行时 preflight + 启动可观测性 (2026-09-19, 已闭合)**:
  08:29 OOM 重启根因 (--max-len 262144 未配 --max-seq 1 → 65GB KV + 84GB
  权重 > 122GB)。新模块 q4t_runtime (memory_budget): 启动前按
  mem_fraction×MemTotal (默认 0.90) 算预算, 权重用 WeightIndex::total_size()
  精确 84GB, 固定成本实测 ~14.2GB, 状态池按 13 层 full-attn 33356B/token/seq
  + 36 层 linear 110.4MB/seq 推导 (max_len, max_seq) 上限; 用户 pin max_len
  推导 max_seq (装不下二分回退 max_len), 未 pin 则 auto 推导 max_len (vllm
  gpu_memory_utilization 等价) → 任何配置都不会 OOM。运行时 preflight:
  trunk 分配前查 MemAvailable (非 MemFree, 稳态 MemFree 仅 2-5GB 会误触发)
  < trunk+draft_logits+2GB → 软降级 plain decode。启动 per-phase 计时
  (PhaseTimer): total 63.7s = model_load 58.3s (90%, 权重单线程 H2D) +
  mtp 2.7s + vision 1.2s。验证: 262144×seq8 (原 OOM 配置) 启动成功 CAPPED
  seq=1; auto seq8 → 53248; 44K 请求 33.3 tok/s MTP 正常; 76 测试全绿零警告。
  下一步: 启动提速 (已闭合, 见下条)。
- **启动提速: 并行 MoE 专家权重加载 (2026-09-19, 冷启动 63.7s→44.2s, 已闭合)**:
  上条 PhaseTimer 定位 model_load 58.3s 占 90%, 其中 MoE 专家读取 49s
  (24576 专家 × 10 小张量, 单线程 host 读 ~1.2GB/s, NVMe 能力 2.7-6.9GB/s
  远未打满)。实现: (1) 每层 512 专家 14 线程池并行 (atomic 工作队列 +
  每线程本地 staging, 零共享); (2) WeightLoader 加 mutex (EnsureOpen LRU
  持锁, 之前单线程无锁) + LRU 8→32 (14 线程并发读多 shard); (3) 零分配
  SwizzleSfInto; (4) 修潜在异步 use-after-free (H2D 未同步即析构 staging)。
  踩坑: 试 OpenAllShards 预开 197 shard 省锁内 LRU 重排 → 热缓存 3x 更慢
  (46→133s, 197 并发 mmap page fault 竞争内核 VMA 锁), 已回退, 保持 32-LRU。
  结果: 冷启动 63.7→44.2s (-31%), model_load 58.3→35.1s, MoE 48 层 17.4s
  (均 363ms/层); **bit-exact** (并行==串行==MTP==plain); 76 测试全绿零警告。
  下一步: 非 MoE 层 ~12.3s + head 1.3s (已闭合, 见下条)。
- **启动提速 (续): pread 读取 + tensor hash 索引 (2026-09-19, 冷启动 44.2s→32.1s, 已闭合)**:
  数据驱动定位: 每层 5120 次 ReadTensor 跨 4 shard (LRU 够), 每 shard
  1504 tensor 但 Find 是线性扫描; 热缓存 363ms/层 = 3.5GB/s 聚合远低于
  带宽 → 算账 1.26GB÷4KB=322K 页 × ~1μs mmap minor fault ≈ 350ms/层,
  与实测吻合, 根因是 mmap 逐页 fault 非带宽/锁。实现 (safetensors.cpp
  Pimpl 内部零 API 变更, MoE+非MoE+head+PLE 全生效): (1) ReadTensor 改
  pread (单次内核调用, 线程安全, 无逐页 user fault, EINTR 重试); (2) Find
  加 name→index hash (Open 时建, O(1) 替代 O(1500))。结果: 冷启动
  44.2→32.1s (-27%), model_load 35.1→27.0s; 非 MoE 层 12.3→4.2s (-66%);
  MoE 17.4→14.3s (现 4.2GB/s 接近 NVMe 带宽)。bit-exact (== C++17 ==
  C++23 基线), 76 测试绿零警告。下一步: MoE 阶段剖析 (已闭合, 见下条)。
- **启动提速 (终): MoE 加载阶段剖析, 确认到达 NVMe 硬件地板 (2026-09-19, 已闭合)**:
  先测量再决定 (避免"计划 D"式优化错目标)。Q4T_LOAD_TIMING 新增
  read/h2d/swizzle 分段: 热缓存 read=80.5% / h2d=16.5% / swizzle=3.0%
  (线程时间); 冷 read 带宽 4.56GB/s **高于冷 NVMe 顺序读 2.72GB/s 1.67×**
  (14 线程并行已榨出 NVMe 并行性)。排除的方向 (全数据驱动): (1) pread
  直接到 device — cudaMalloc 指针 pread 4KB 实测 **EFAULT**, Thor 内核
  pread 页错误写路径不可写 device 内存; (2) 更多线程 — Q4T_MOE_THREADS
  冷测 14 与 32 **逐毫秒相同** (NVMe 已饱和), 64 更差 (过订阅); (3) 合并
  同类型连续读 — 会失去跨 shard 并行性退化为顺序读, 更慢。结论: MoE
  84GB 冷 ~18.3s = NVMe 硬件地板, 软件优化空间闭合; 冷 model_load 27.1s
  = head 1.4 + MoE 18.3 + 非 MoE ~7 + ple 0.1。再快需架构级 (expert 按需
  流式, 启动换推理延迟), 属 Phase 级决策。保留 env 门控工具
  (Q4T_LOAD_TIMING 分段 / Q4T_MOE_THREADS 线程数), 默认行为不变,
  bit-exact + 76 测试绿零警告。
- **chunked MTP: 长上下文投机解码 (2026-09-19, 44K 17.7→33.1 tok/s, 精度无损, 已闭合)**:
  前序发现 plain decode 单步 4K≈44K (57ms, 不随上下文退化), 4K=30 vs 44K=17.7
  差距纯粹是 MTP gate (44K chunked 被禁)。旧 gate 按 262K 最坏算过保守:
  draft KV/indexer 池在 MTP load 时已按 max_len×max_seq 分配 (非 per-request),
  per-request 新增只有 trunk buffer (44K 0.9GB)。实现: MtpDraftExtend 分块
  (每块 ≤ max_prefill 调 MtpForward, 仅末块 compute_logits, 省 21.9GB;
  draft 单层无 SSM, KV 绝对位置 per-seq 池化, 块间天然续接) + chat_server
  chunked prefill 累积 trunk (ModelPrefill/ModelDecodeBatch 传 trunk_out) +
  use_mtp gate 改 d_trunk_full != nullptr (分配失败软回退 plain)。验证:
  44K 分 6 块全 ok, decode 33.1 tok/s (4K MTP 33.6 持平), **44K 同 prompt
  MTP vs --no-mtp 输出 bit-exact** (主模型验证所有 draft token), 76 测试
  零警告。运维: 本次 --max-len 262144 未带 --max-seq 1 → KV 池 65GB OOM
  重启, 规则已记 (serve 显式 --max-seq 1 + max-len 按需最小, 44K 用 49152)。

- **长上下文 decode 性能: one-pass 全块打分 + 多级并行 top-k (2026-09-19, 44K 14.4→17.7 tok/s)**:
  块并行 indexer (0ae1304) 后 44K decode 14.4 tok/s, 用户要求推回 18+ 且不改
  精度 (全块打分召回保留)。关键洞察: 流式 top-k 是为 prefill 设计的, decode
  T≤4 时 [T,n_groups] 才 ~180KB 根本不需要流式 — 一次打完所有块 + 多级并行
  top-k。4 新 kernel (OnePassScore warp-per-block coalesced / SliceLocalTopk /
  WindowMergeTopk 逐级收敛 / FinalTopkExpand), 数学保证全局 top-512 ⊆ 各窗口
  top-512 的并。自检 id_diff=0 (n_groups=10966), 76 测试零警告, 4K 无回归
  (30-34)。单步 nsys: indexer 24%→7%, 瓶颈转 Fp8Gev 31.2% (权重带宽地板)。
  测试期间机器重启 (两模型实例并发 OOM, 规则已记 repo memory)。

- **QSA 长上下文召回修复: 流式 top-k (2026-09-18, 移除 8192 硬上限, 已闭合)**:
  用户真实场景 = agent 开发 (40K-200K token prompt)。发现 QSA 只 attend 前 8192
  token: `kMaxBlocks=2048` 把候选块锁死在前 2048 压缩块, 8192 之后的近期上下文
  **从不被打分** (needle 13.5K 末尾密钥答不出)。这是**我们的不完整实现 bug**
  非 SGLang 设计 — 参考 (tokenspeed/vllm/sglang-ssd-stream) 都对全部 num_blocks
  打分无上限。prefill 不能 materialize [T,all_blocks] (T=8192 时 3.3TB), 故镜像
  tokenspeed split+merge-tree 做**流式 top-k**: CHUNK=2048 循环覆盖所有块, 每块
  tensor-core GEMM 打分 + merge 进 running top-512, 末尾 expand + 当前 group
  尾部。`kMaxBlocks` 语义改"打分 CHUNK 大小" + 新增 `kMaxBlockTopk=512`;
  IndexerLogits/Reduce 加 `block_off` 打分全局块去 cap; 新增 BitonicSortAsc/
  InitRunTopk/MergeChunkTopk/ExpandRunTopk 4 kernel; ≤8192 走**原 single-shot
  路径不变** (零回归), >8192 走流式。**验证**: (1) 流式合并=精确全局 top-512
  (host 暴力自检, 多 chunk id_diff 全边界 near-tie, far_from_boundary=0,
  max_logit_diff≤0.00011); (2) **needle @9000 (group 2250>2048, 旧代码 100%
  不可见) 召回** — 硬上限移除的决定性证据; (3) 76 测试全绿零警告, 短上下文
  零回归。已知边界 (非 bug): QSA top-512 是选择性稀疏, 块数远大于 512 且 needle
  不够突出时可能跌出 (参考无大 recent window, 与 vllm/sglang 一致)。
- **decode 全面推进: HC mix FP8 (2026-09-18, 最大遗漏)**: 评估剩余杠杆后发现 HC
  (hyper-connection) mix_down[320,10240]+mix_up[10240,320] 每层 attn+mlp ≈ 1.27GB
  BF16/step (和 lm_head 一样大) 从未转 FP8。加 Fp8Part::kHc + ProjGemm, 纳入
  Q4T_FP8_PROJ。decode 22.1→22.8 tok/s (+3%), 质数事实 prompt 逐字一致。**FP8 投影
  覆盖已全面** (大投影全转); 剩余 Bf16Gev = 小 latency-bound GEMV 各<1%。单流 decode
  近结构地板。下步: M≥8 tensor-core FP8 (大批, 需 mma) / MoE 融合 (难)。
- **decode Q2: FP8 批处理 decode (2026-09-18, 手写胜 cuBLASLt 于 M≤4)**: ProjGemm 仅
  M==1 走 FP8, 批处理 decode (连续批处理打包 M=B) fallback cuBLASLt BF16 丢 FP8。
  实测手写 W8A16 vs cuBLASLt BF16 (tools/small_m_gemm_bench): M=1 1.9-2.1× / M=2
  1.7-2.0× / M=4 1.2-1.5× / M≥8 cuBLASLt 胜 (SIMT 转算力受限, tensor core 胜)。
  新增手写 Fp8SmallMKernel (warp-per-output 权重读一次 M 点积驻寄存器) + ProjGemm
  分发 (M==1→Fp8Gev / 2≤M≤4→Fp8SmallMGemm / M≥5→cuBLASLt)。与逐行 Fp8Gev 逐位一致
  (l2rel 0), 76 测试, E2E 三并发连贯 ~32 tok/s。Q1 QSA 延后, Q3 MoE 融合待后续。
- **FP8 审计响应 + max_seq 修复 (2026-09-18)**: (1) 求证 NVFP4=W4A4 (act_quant.cu
  运行时量化激活), 与投影 W8A16 区分。(2) 审计两点已落地: **拆分开关**
  (Q4T_FP8_ATTN/_GDN/_LMHEAD/_SHARED 各自独立, 可单独测收益) + **真实 FP8 路径
  单测** (tests/gemv_fp8_test.cpp 4 个经 ungated QuantizeToFp8Shadow 真执行
  Fp8GevKernel: 量化/噪声带/调度/argmax, 75 测试全绿)。(3) **serve --max-seq 2
  崩溃修复** (compute-sanitizer: QSA indexer GEMM 读 idx_comp 越 max_len slice;
  max_blocks=min(kMaxBlocks,max_len); 既有 bug 非 FP8 回归)。
- **FP8 W8A16 decode 投影 (2026-09-18, 正结果 1.31×, opt-in Q4T_FP8_PROJ)**:
  用户"目标单流 decode 达 273 GB/s 上限"。nsys 先测: `Bf16GevKernel` (M=1 投影
  GEMV) = decode **61.6%**, **GPU 满载非 launch-bound** (kernel busy≈wall,
  cudaLaunchKernel 1.6% 隐藏) → CUDA Graph 无用。proto 复测: **BF16 GEMV 大投影已
  244-252 GB/s** (近 273 spec) → **认知修正: 杠杆非"提高 GB/s"而是 FP8 减字节**。
  实现 W8A16 (e4m3 权重 1B + per-channel scale, BF16 激活): `Fp8GevKernel` +
  `QuantizeFp8RowKernel` + `Fp8Shadow`/`BuildFp8Shadow` + `ProjGemm` (M=1 有
  shadow→FP8, 否则=Bf16Gemm)。接入 attn q/k/v/o + GDN in_proj_qkv/z/out +
  lm_head + MoE shared (router/index_qk/a/b/HC 保 BF16)。**双存零风险** (BF16 权重
  不动, prefill bit-identical; FP8 shadow 纯附加, 仅 M=1 用)。**结果**: decode
  17.9→**23.4 tok/s (1.31×)**, greedy 输出对事实 prompt 与 BF16 **逐字一致**,
  serve API E2E 3 请求全连贯无 error ~22.7 tok/s, 71 测试无回归。默认 OFF
  (保 BF16 精确路径为默认; 翻默认 ON 待更广质量验证)。下一杠杆: MoE quant
  kernel 融合 (SwiGLUQuant+GatherQuant 11.2%)。
- **serve 工业级审计 + 加固 (2026-09-18, DoS/健壮性)**: 系统审计 chat_server.cpp
  (1651 行) + 调度器。已具备: header 1MB/body 16MB guard + conn_cap 503 shed +
  AllocSeqId 排队背压 + 共享 prefill buffer + lockstep 调度 + drain。**已修缺口**:
  socket 读写超时 (SO_RCVTIMEO/SNDTIMEO 30s, 防 slowloris) + max_tokens 上界 (cap
  max_len, 999999→249 实测) + 客户端断开检测 (stream WriteAll 返回) + backlog 128。
  **P2 已修 (推进)**: 信号优雅关闭 (SIGINT/SIGTERM → RequestStop → StopScheduler +
  drain in-flight ≤15s → 干净退出, 防 detached 线程 UAF) + GPU 健康上报 (forward
  CUDA sticky error → gpu_healthy_=false → healthz 503 + 拒新请求, orchestrator 可
  **P3 已修 (参考 vllm 指标集)**: Prometheus /metrics endpoint (counters:
  requests_total/success/error/aborted + prompt/generation_tokens; gauges:
  num_requests_running/seq_slots/gpu_healthy; histograms: ttft/e2e/queue_seconds),
  lock-free atomic + HandleChat 埋点。**剩余** (低优先): 深度 healthz JSON /
  keep-alive。71 测试全绿, SIGTERM 排空 + /metrics (3 请求→计数正确) 验证。
- **PLE page_reader io_uring exact-recovery (2026-09-18, 从 ds4 借鉴)**: PLE SSD
  Stream 对标 ds4 (同模型+同硬件类) / tokenspeed (datacenter 无 SSD stream) —— 我们
  核心机制领先/对齐 (io_uring registered pool + page dedup + sglang 参考)。唯一借鉴
  = ds4 磁盘读 exact-recovery: page_reader 从 fail-closed 升级为瞬时错误 (EAGAIN/
  EINTR/EBUSY/ECANCELED) 重试 + 短读补齐 (kMaxReadRetries=8), 持久 errno/意外 EOF
  仍 fail-closed。正常路径逐位一致 (ple_e2e l2_rel 0), 故障注入测试
  ple_page_reader_fault_recovery 验证。71 测试全绿。
- **GatedDeltaNet 寄存器-state prefill kernel (2026-09-17, 正结果 +12%, 默认开)**:
  用户加 ds4/tokenspeed 参考。**对标: 我们单流 prefill 已超 ds4 30-50%** (同模型
  Qwen3.8 Flash Next + 同硬件类 DGX Spark GB10, ds4 745-771 vs 我们 989-1143 t/s,
  Spark 带宽还略高)。唯一新杠杆 = ds4 gdn_scan 寄存器-state: default GatedDeltaNetKernel
  只 nv=48 blocks (20 SM 不足) + shared S[kd,vd] 66KB → 25% 占用率锁死, 单流 GDN
  latency-bound (DRAM floor 12.5ms vs 实测 700ms = 56×)。**warp-per-vd-column** (每 warp
  ROWS 列, kd 分布 32 lanes + warp-reduce, state 驻寄存器 s[ROWS][4], grid=(vd/(4*ROWS),
  nv))。陷阱: 初版把 L2 norm 融进 scan → 每 head 64 warp 冗余算 → GDN +8.5%; 修复拆独立
  **GdnRegPrepNormKernel** 一次性 normalize (仅 0.3%)。ROWS sweep: 1=977/2=1221/4=1292/
  **8=1318** (ROWS 越大 ILP 越隐藏 warp-reduce latency)。**nsys T=8000: GDN kernel
  4.20→2.67s (-36%), 整体 prefill 1147→1283 tok/s (+12%, T=3000 同), ptxas 0 spill**;
  GDN 27.2%→18.9%, **SparseAttention (28.7%) 重回唯一 #1 瓶颈**。默认开 ROWS=8
  (Q4T_GDN_REG=1/2/4 override, =0 回退 shared); gdn_reg_rows 非 static 供测试 toggle;
  4 批量/多序列/MTP 等价测试用 GdnRegOff RAII 临时关 reg (单序列 golden 与批量 shared 同
  kernel), reg 正确性由 linear_attention 覆盖; 70 测试全绿。**修正旧结论"GDN 近占用率
  上限 25%"** (那是 shared-state thread-per-vd 布局上限, 寄存器 warp-per-column 突破了)。
- **prefill/decode 融合: 原语保留 (正结果 bit-identical) + 调度器融合无收益 (负结果,
  已回退) (2026-09-17)**: 用户授权建融合 (确认加法式, 不破坏 PD 分离)。**增量 1
  ModelMixedBatch** (泛化 ModelPrefillBatch: per-seq base_position reset-or-continue +
  统一 PLE history): bit-identical (全新 batch == ModelPrefillBatch l2_rel 0; 融合
  continue 行 == 全量 ModelPrefill 末行 0, argmax 一致), **已提交 1af0821 保留**
  (PD-ready co-located 原语, 当前未调用)。**增量 2 调度器融合**: prefill+decode 共现
  融合成一次 forward。发现调度器 (opportunistic prefill + lockstep decode) 使两者
  **时序不相交, 融合从不触发**; 加 fuse-align 强制触发后**吞吐持平偏略降** (饱和 24
  req: ON 60-65 vs baseline 65-66 tok/s; prefill 重: 54.5 vs 54.8)。**无收益, 已回退
  调度器改动**。根因: **Thor 均匀带宽受限** (FP4 张量核弱 vs 241 GB/s LPDDR5x,
  roofline T=2560 prefill 仅 1% FP4 峰值), prefill 与 decode **都** memory-bound →
  **无 compute/memory 流水线不平衡**可配平 (H100 上 prefill 计算受限+decode 带宽受限
  才有), 融合只让 forward 更重被抵消。**本 session 第 4 个配平/合并负结果 (gather /
  grouped-GEMM / 分块交织 / 融合), 全同根: 已近结构带宽地板**。
- **长 prompt prefill: SparseAttention cp.async 双缓冲预取 (2026-09-17, 正结果 +20%)**:
  full-model nsys 定位长 prompt (T=8000) prefill 瓶颈 = SparseAttentionKernel 38%
  (随 T 涨), latency-bound (散射 paged-KV gather 与 compute 串行)。用 cp.async 双缓冲
  预取 chunk c+1 的 KV 与 chunk c 的 QK/PV 重叠 (纠正旧误判"cp.async 不支持 paged
  间接寻址"——能)。**bit-exact** (staged 值不变, full_attention l2_rel 4.688e-3 不变),
  **占用率零代价** (56 reg 不变 register-limited 4 block/SM, shared 26→42KB 仍 4)。
  **SparseAttention GPU 时间 −40% (6.99→4.20s), prefill T=8000 942→1128 tok/s (+20%) /
  T=2560 ~1061→1188 (+12%)**, decode 亦受益, 69 测试全绿输出连贯。GatedDeltaNet 现
  并列 #1 (26.8%)。教训: 先 full-model profile + ptxas 查占用率 → 直接命中正结果。
  **补充: QK 2-warp 并行 (+1.3%, T=8000 1128→1143)** —— prefetch 后 QK (warp 0 独占)
  暴露, 拆 2 warp 并行 (bit-exact); 收益小证 kernel 已 latency-bound, SparseAttention
  近地板 (累计 +21.3%)。GatedDeltaNet (26.8%) 是死路 (vd-split ≈0 破 bit-exact,
  chunked tensor-core Thor 上更慢); 剩余大杠杆仅 query-tiling (大改, 不确定)。
- **grouped FP4 MoE GEMM = 无预期收益 (2026-09-17, 负结果探针, ① 否决)**:
  tools/moe_grouped_bench.cu 真实 MoE 维度直接探针 (E=512, gu[1280,2560]+dn[2560,640]
  FP4, all-E 1.42GB>>L2), 三路对比 per-expert cuBLASLt 1-str/4-str vs 纯权重流式地板。
  **现有 4-stream+plan-cache per-expert 已 ~233 GB/s = 流式地板 253 的 92% / spec
  241 峰值的 96%, 所有 M_e (1-64, B=1 decode→prefill) 都近地板** → CUTLASS grouped
  GEMM 天花板仅 **~1.1×** (也必须每 expert 权重读一次)。多流 MoE 优化 (2026-09-17)
  实质已吃掉收益。文档"MoE 34% 带宽"误导 (GEMM 部分实测近峰值)。**不值得高风险
  CUTLASS-on-Thor 投入**。与 gather 同: MoE per-expert 已近结构地板非 launch-bound。
- **下一杠杆调研 + FP8 W8A16 投影 GEMV spike (2026-09-17, 正结果 Thor ~2×, 用户
  现阶段不做权重量化搁置)**: ② FP8 投影 spike 证 ~2× (tools/fp8_gemv_proto.cu),
  但用户现阶段不量化权重 → 搁置。① CUTLASS grouped FP4 见上, 已否决。
  **单请求性能已近结构带宽地板** (prefill ~1000 / MoE GEMM near-floor / attn+GDN
  near-floor); 剩余方向 = 连续批处理聚合 (已 300+) 或 Phase 2 功能 (262K/PD)。
- **grouped GatherQuant = MoE gather 是 work-bound 非 launch-bound (2026-09-17, 负结果,
  已回退)**: 前提 (profile GatherQuant 6.0% / ~29K per-expert 启动 → 合并 1 次启动省
  5-6%) **证伪**。实现 grouped gather (一次启动量化全部 R 路由行, per-row 与 per-expert
  逐位一致, `quant_moe_gemm max_rel=0.0077468` 完全一致), A/B: 2560 tok 1061.5 vs 1061.2,
  8000 tok 936 vs 942 (**略慢 0.5%**)。根因: GatherQuant 6.0% 几乎全是量化 WORK
  (memory-bound), grouped 做同样 work 只省 <1% 启动开销; decode (M=1) 只激活 k≈8 experts
  无可合并。**MoE 真杠杆 = grouped/monolithic FP4 GEMM** (E 次 cuBLASLt → 1 次 CUTLASS
  group-GEMM, 命中 FP4 GEMM 13.7%), 需 CUTLASS group-gemm (独立大 session); grouped gather
  是其必要 substrate 但单独无用, 应与 GEMM 一起落地, 不留 default-off dead code。
- **serve decode = GPU-forward-bound (2026-09-17, 负结果结论)**: 尝试 scheduler-driven
  decode (调度器内部驱动整个 decode 循环, 消除每步 64 线程握手) A/B 无收益 (C=64
  265 vs 262 +1.3% 噪声内, 已回退)。20W GPU + 46% CPU 都不饱和不是步间协调 gap, 而是
  **单步 decode forward 本身 memory-bound**。调度层已榨干 (lockstep+argmax+批量 prefill)。
  **最大杠杆 = decode forward → monolithic grouped FP4 MoE** (移植 flashinfer trtllm)。
- **serve 集成批量 prefill (2026-09-17, 已落地)**: 并发 plain 请求 (非 vision/chunk/MTP)
  的 prefill 注册到调度器, 打包一次 ModelPrefillBatch (dense 权重读一次); B=1 孤立请求
  特判走单序列 ModelPrefill (与 inline 位级一致, 保行为不变), 只有 B>1 才批处理。
  **serve A/B**: prefill-heavy max_seq=16 mt=8 C=16 38.3→**76.4 (2.0×)**; **C=64 mt=64
  聚合 181→262 (+45%, batched prefill 真实大杠杆)**。B=1 孤立 2+2→"Four" 位级一致;
  C=32 超限存活 0 error, 69 测试全绿, 长单序列 prefill 1058 tok/s 不变。
- **ragged 批量 prefill 原语 + ModelPrefillBatch (2026-09-17, 已落地)**:
  serve 剩余差距主要在 prefill 阶段 (并发请求各自扫 ~24GB dense 权重)。多流已在 N=4
  饱和 (prefill N=4/8/16=1061/1061/1062 tok/s), 真正杠杆是**打包并发 prefill 一次
  forward**。把 5 个 MTP 多序列 causal kernel 泛化到变长 (新 `RaggedBatch` 描述符:
  cu_seqlens + token_local; token-grid 只换局部位置, batch-grid 用 off/len;
  full attention 已 per-token 驱动无需改)。**dual-path: ragged==null 走原路 → MTP
  位不变**。`ModelPrefillBatch(tokens,lens,seq_ids,B)` 打包 B 变长全新序列一次
  forward。新测试 B=3 (len 5/3/7) vs 独立单序列 prefill **逐 token l2_rel=0.00000
  位级一致**。69 测试全绿。**bench-prefill 实测 (prompt=128): 顺序 vs 批量 speedup
  B=2/4/8 = 1.46×/1.82×/2.39× (权重摊销, 未饱和)**。下一步: 调度器收集并发 prefill →
  一次 ModelPrefillBatch, 以 serve 端口衡量。(注: bench 合成 token 触发 ModelPrefill/
  MoE 既有 latent 小 dim 问题, bench-decode 同复现, 非 ragged 代码, 真实 serve 无。)
- **plain decode 调度 lockstep (2026-09-17, 已落地)**: B2b 调度器 plain 调度从机会式
  "任一 pending 即跑"改为 lockstep "所有活跃 pending 才跑" (对齐 MTP plan A +
  vllm/sglang), 批 B=活跃请求数 uniform 而非 ragged (每小步重读 84GB 权重)。
  **serve 并发吞吐 +26~53%** (8→65.2/32→137.4/64→173.4 tok/s), scale 8.3→**10.4×**。
  正确性保持 ("Four"), 128 并发排队存活 0 error, 68 测试全绿。
- **路径 C 兑现 (2026-09-17)**: serve 并发吞吐 concurrency 1→64 = **16.6→138.1
  tok/s (8.3×)** 且仍爬升, 0 error 存活。与 bench 理想 18.6× 差距 = prefill +
  HTTP/tokenizer 串行 + 调度非 lockstep 开销。缩小方向 (不量化): prefill
  last-row lm_head / prefill 批处理 / tokenizer 并行。
- **serve 资源背压 (2026-09-17, 已修)**: 并发压测崩溃 (per-request ~1GB d_logits ×
  并发 OOM) → 共享 prefill buffer (一次分配, 持锁内读首行) + 阻塞 AllocSeqId (超
  max_seq 排队非 503) + 连接上限 (防线程爆炸)。压测 2×/4× 超限 + 16 并发长 prompt
  内存稳、0 error、存活。**请求超资源现排队不崩溃**。
- **多流 MoE (2026-09-17, 默认开, `Q4T_MOE_STREAMS`=4)**: per-expert chain 无依赖
  → 轮询到 4 个 CUDA 流并发填满欠占用的 SM (小 GEMM M_e≈10-50 只填半数 SM)。
  **prefill 990→1062 tok/s (+7.2%, 破 1000)** + 批 decode B=128 308→374 (+21%) /
  B=32 +23%。persistent scratch 是关键 (per-call 32MB×3 gemm_ws 分配会把收益
  吃掉 → 静态池)。数值逐位不变, 68 测试全绿, N=4 甜点 (N=8 持平)。
- **批处理 decode 标度实测 (2026-09-17, `q4t bench-decode --sweep`)**: 聚合吞吐
  B=1→128 = **16.6→308.9 tok/s (18.6×)** 且仍在爬升。ms/step 仅 60→414 (128×
  token 6.9× 时间) → per-token 成本降 18.6× = MoE 权重摊销 (roofline 预言兑现)。
  B=128 瓶颈: GatedDeltaNetDecode 34.3% (SSM state 读写 86% 带宽 = 地板) + MoE
  fp4 GEMM 24.4% + 量化 16.4% (per-expert, MoE 仅 ~34% 带宽)。多流已吃到 SM 欠
  占用那部分; monolithic grouped FP4 MoE (host launch + 权重带宽) 是更大杠杆。
- **根本性结论 (2026-09-16 roofline, tools/roofline_prefill.py)**: prefill 总算力
  28.2 TFLOP, 达到 10.8 TFLOPS = **FP4 峰值 1.0% / BF16 4.2%** → 张量核 ~96% 空转;
  纯算力下限仅 0.03s 而实测 2.6s = 86× → wall time 几乎全是访存+延迟。
  MoE routed AI=200 ≪ FP4 ridge 4295 (512 专家×~50 token/专家, 权重带宽受限);
  标度曲线 T=1280→1012.7/2560→989/5120→950.7 tok/s (随 T 降 = per-token 访存
  主导, 非权重摄销受限)。**空转张量核是访存受限的症状, 非算力浪费**;
  填满它靠连续批处理聚合吞吐 (单请求已近结构天花板 ~1000 tok/s)。
- **基线**: prefill 2560 tok ≈ **989 tok/s** (原始 220–437, 累计约 2.3–4.5×,
  已达 vllm 1k+ 的 ~0.99×)。nsys (最新): SparseAttention 26.8% / GatedDeltaNet
  24.4% / MoE gather+swiglu ~8.5% / MoE FP4 GEMM ~10% / MoE combine 1.6% /
  HC combine 4.0%+mix 3.1% / conv 2.5% / Indexer 张量核化后 0.1%。attn/GDN 近地板。
- **已闭合**: Step 8–9 SparseAttention tensor-core + 寄存器累加器 (1264→60
  ms/call, 21×); Step 10 GatedDeltaNet 内循环 ILP + warp 归约 (786→826);
  MoE 去死写 (compact/inter, 826→871) + MoE 中间量 bf16 (Fp4Gemm 模板化,
  871→893) + QSA indexer logits 张量核化 (SIMT 三重循环 → Bf16Gemm + reduce,
  893→947) + MoE combine 批量化 (per-expert scatter 29178 launch → 单次确定性
  gather, combine GPU 时间 287→87ms, 947→981) + HC combine inject 门控预计算
  (去 hs 倍冗余 sigmoid, CombineWithGate 244→211ms, 981→989)。
- **chunked tensor-core GatedDeltaNet: 负结果** (2026-09-16)。算法/GEMM/完整
  kernel 三步验证全绿并集成 (flag `Q4T_GDN_CHUNKED`, 工件 tools/gdn_chunk*_
  proto.cu), 但 Thor 上更慢 (kernel 1.96s vs SIMT 1.35s): 20 SM + 单序列小
  matmul 不饱和 tensor core + state 驻 shared 锁占用率。默认保持 SIMT。
- **下一杠杆候选**: MoE gather/swiglu quant 仍 per-expert (29353 launch, 但
  med 5.9/6.1µs > launch 开销做真实量化工作 + per-expert SF swizzle 128 对齐
  受阻, 批量化收益小) / 完整 grouped FP4 GEMM (受 SF swizzle 约束, 大改) /
  SparseAttn FP8 KV (27%, 现 71% 带宽, vllm 主 attention 仍 bf16 质量风险)。
  GatedDeltaNet 已近 SIMT 占用率上限。

## 进行中

- **长上下文 262K (262144) 验证 (2026-09-14 启动, 内存实测 + 分块 prefill
  均已闭合)**: 模型声明 `max_position_embeddings=262144` (config 已解析)。
  **实测 (2026-09-15, 带内存看门狗): `--max-len 262144 --max-seq 1` serve
  加载成功, 峰值可用内存 25.6 GB (消耗 ~96 GB, 无 OOM), 短 prompt 生成
  正常且确定 (2/2 一致), 请求后无泄漏 (25 GB 稳定)** — 与预算估算
  (~101 GB) 吻合。max_seq=4 仍 OOM (~158 GB, 此前实测崩机)。**分块
  prefill 已实现并验证 (2026-09-15): `max_prefill` 改分块大小, 分块路径
  Prefill+DecodeBatch 续块, 正确性 argmax 一致 (差异在引擎固有非确定带),
  200K-token prompt E2E 无 OOM 生成正常**。prefill 吞吐受 MoE GEMM 带宽
  下限约束 (~12.6s/2048-token 块, 262K ≈ 27 分钟)。模型层硬伤 (如实
  报告): QSA idx_budget=2048 在 262K 只 attend ~3% 历史块, 召回受限。
  详见 PHASES.md "长上下文 262K 内存预算" + LOG 2026-09-15。
- **完整 PD 分离部署 — 降级为后续计划 (2026-09-14 用户决定)**: 当前
  PD-ready 架构 (Paged KV + 可分离路径 + ModelSequence 阶段边界 API)
  已满足本机调度需求 (持续 prefill 场景靠 Paged KV 按页迁移 + 独立调度
  池); 多设备/双机扩展 (KV 跨设备传输) 归后续计划, 需多卡硬件验证。

## 阻塞 / 风险

- **PD-ready 架构 (Phase 1 可分离性已全部完成)**: runner 后期特殊
  场景需 PD 分离, 架构须早期可分离。Phase 1 落地可分离性:
  ✅ Paged KV cache; ✅ prefill/decode 可分离代码路径; ✅ 阶段边界 API
  (ModelSequence, 引擎暴露"完成 prefill、交出 KV/SSM 状态"为独立操作)。
  完整多设备 PD 部署 (连续批处理 + 多请求调度) 归 Phase 2。
  设计见 ARCHITECTURE.md, 范围见 PHASES.md 第 6 项。
- **MTP 已完成 (2026-09-07, 加速比 2026-09-10 确认)**: draft k 步 + 主模型
  验证 + 接受/回退 + per-token SSM/conv checkpoint 恢复 (消除部分接受
  re-advance), 实测 decode 加速 1.46x (k=3 最优, 默认 mtp_k=3), 见
  "已完成" MTP 条目 + LOG.md 2026-09-10。
- **视频输入 ViT (2026-09-11, Phase 2 启动)**: 27 层 ViT 扩展支持多帧视频。
  核心差异 (参考 vLLM qwen3_vl.py): **逐时间组注意力** (cu_seqlens=repeat(h*w,t),
  每 2 帧组内 h*w 空间 patch 各自双向注意力, 组间不 attend; t=1 退化为图像
  单一注意力) + **pos_embed/RoPE time-major 平铺 t 次** (无独立时间维 RoPE)。
  `AttentionKernel` 改分段 softmax, `BuildPosTables`/`BuildPosIds` 加 t 平铺。
  验证: image l2_rel=0.0317 (无回归) / **video (t=2) l2_rel=0.0206** (纯 BF16
  精度)。踩坑: numpy 参考 t 平铺误用 `np.repeat` (逐元素) 而非 vLLM 的
  `.repeat(t,1)` 块重复 (= `np.tile`), t=1 相同故图像一直通过, t=2 暴露;
  逐层 dump + pos_ids 对比确认 bug 在参考不在 C++, 已修。62 测试全绿零警告,
  见 LOG.md 2026-09-11。
- **视频输入 processor (2026-09-11, Phase 2)**: `ProcessVideo` (帧解码 →
  视频 smart_resize [3-D t*h*w 预算, 独立 video_preprocessor_config.json
  边界 4096/25165824] → 逐帧 BICUBIC → 奇数帧 pad 末帧 → 时间维 patchify)。
  **关键**: 视频 resize 用 torchvision BICUBIC (图像用 Pillow), 实测差异
  max~0.016/l2_rel~0.001 (远小于 BF16 带) → C++ 复用 Pillow 定点 BICUBIC +
  容差差分 (≤0.02/≤0.005)。6 case 全过 (偶数/奇数帧/大分辨率/BICUBIC,
  identity 逐位 + BICUBIC 容差)。踩坑: 时间组索引偏移 `g*gh*gw` (初版误乘
  M*M, grid_t>1 全错位)。63 测试全绿零警告, 见 LOG.md 2026-09-11。
- **serve 层视频接入 (2026-09-12, Phase 2, 视频输入 3/3 闭合)**: HTTP API
  接入视频。`VisionItem` (kImage 1 帧 / kVideo N 帧, 按 content-part 顺序) +
  `RunVisionPipeline` 混合 batch (图像 `ProcessImage` 图像预算 / 视频
  `ProcessVideo` 视频预算, 共享一次 `VisionForward`) + `ExpandMultimodalTokens`
  (图像/视频独立计数按位置展开)。视频 part: `{"type":"video",
  "video_frames":[base64 data url...]}`。**修复既有图像 bug**: 多模态占位符
  实为 `|image_pad|` (248056) / `|video_pad|` (248057) (hex+round-trip 三方
  验证), 旧代码塞 `<image>` 被 BPE 成 3 token → 图像 HTTP 路径一直 400
  (此前"已闭合"是手构 input_ids 的 e2e, 非 HTTP)。E2E: 图像 72=8+64 (输出
  "green" 语义正确) / 视频 21=13+8 / 混合交错 85=13+64+8 (位置顺序正确)。
  63 测试全绿零警告, 见 LOG.md 2026-09-12。
- **serve 层 MTP (2026-09-11)**: HTTP API decode 接入 MtpSpeculativeStep
  (复用 CLI 已验证机制), 失败自动回退 plain; 端到端 decode ≈ 17.5 tok/s
  (~1.4x, 与 CLI 一致), 流式 SSE 正常, 见 LOG.md 2026-09-11。
- **serve 层长上下文 (2026-09-11)**: serve 加 `--max-prefill N` flag
  (ServerOptions.max_prefill, 默认 0=ModelConfig 2048; MTP 侧同步主模型,
  顺带修正此前 serve MTP 工作区未同步的问题)。4K 长 prompt (4340 tok)
  HTTP 验证通过: 200 + 连贯输出 (修复前 >2048 直接报错), 见 LOG.md
  2026-09-11。
- **长上下文 decode 退化根因修复 (2026-09-11)**: 用户诊断"decode 随长度
  退化像 full attention"正确。nsys 定位真因 = `TopkSelectKernel` 旧单线程
  O(block_topk×n_groups) 扫描, 占 4K/8K decode GPU 时间 61%/73% (11.7/20.4ms
  每次), **非 KV 读** (此前归因误判, 已更正)。重写为并行 bitonic sort
  (2048 槽 shared, 256 线程, ~20µs), 选中集合不变 (顺序无关, online softmax
  归约)。修复后 **4K 4.3→10.7 / 8K 3.0→10.3 tok/s, 且 4K≈8K 不再随长度
  退化** (稀疏注意力预期行为); 短序列 16.5 tok/s 无回退; 62 测试全绿零警告。
  **kernel 级 nsys 确认**: TopkSelectKernel 11.68ms→82.3µs/次 (142x), GPU
  占比 61.3%→1.1%; 新瓶颈 Bf16Gev 41% + SparseAttention 31% 均不随长度增长,
  见 LOG.md 2026-09-11。
- **长上下文验证 (2026-09-11, 1.7K/4K/8K)**: QSA 稀疏路径 plain + MTP 均
  通过, 输出连贯。TopkSelect 修复后 MTP 复测: 4K 1.54x / 8K 1.49x (修复前
  1.79x/1.87x, 加速比因 plain 基线抬升而收敛), 但 MTP 绝对 decode 大涨
  (4K 7.7→16.5, 8K 5.6→15.4 tok/s), 4K/8K 均不再随长度退化。CLI 加
  --max-prefill flag (默认 2048 不变); decode 上限 max_len=8192; 262K 为大
  工程 (KV ~64GB + QSA idx_budget 瓶颈), 见 LOG.md 2026-09-11。
- **kernel launch 削减 (2026-09-10, 性能中性)**: conv1d+checkpoint 三合一
  (CausalConv1dWithCkptKernel, num_ckpt=0 退化纯 conv) + HC gate/combine
  融合 (CombineWithGateKernel, gate 寄存器内重算位级一致) + PLE conv+add
  融合 (DepthwiseConvAddKernel, conv 寄存器内加 gated 位级一致, 省 d_conv
  buffer) + full_attention q/k norm 合一 (QKDeinterleaveNormKernel, 不相交
  头按 blockIdx 分支, 位级一致) + PLE trunk_add 融合 (PleLayerForward 加
  trunk_add 可选参数, 双重 BF16 舍入位级一致, 删 PleAddTrunkKernel)。5 个
  融合共减 ~97 launch/forward, 62 测试全绿零警告, 干净基准 plain 14.5 tok/s
  零回退, 见 LOG.md 2026-09-10。
- **MoE 贪心非确定性** (见"进行中"长序列条目): `ScatterAddKernel` 的 FP32
  `atomicAdd` 顺序非确定, 运行间 argmax 可能翻转。属 LLM 固有特性 (PyTorch
  同样), 不影响正确性; 如需可复现输出, 可改确定性归约 (代价: 性能)。

## 下一步

> 历史"下一步"与性能优化阶段 A–J 已归档至 [DONE.md](DONE.md) (2026-09-20)。
> 以下为当前真实在途方向。

- **性能优化 (Phase 3 候选)**:
  - **decode**: 单流非量化 (BF16) 已到内存带宽下限 (Bf16Gev 240-260 GB/s =
    100-108% LPDDR5x 峰值, 2026-09-19 实测), kernel 无优化空间。唯一再快
    杠杆 = 减少权重字节 (FP4/FP8 量化, 用户已排除非量化约束) 或放宽单流
    (B>1 批处理, 已闭合 B2)。FP8 W8A16 decode 投影 (opt-in `Q4T_FP8_PROJ`,
    1.31×) 已落地, 翻默认 ON 待更广质量验证。
  - **prefill**: SparseAttentionKernel 是 #1 瓶颈 (长 prompt 71.4%), 已
    profile (cp.async 双缓冲 +20% / 2-warp QK +1.3% 已落地), 进一步优化
    待对照 flashinfer/FA4 参考。
- **长上下文 262K**: 内存实测 + 分块 prefill 已闭合 (2026-09-15)。剩余为
  **模型层硬伤** (QSA `idx_budget=2048` 在 262K 只 attend ~3% 历史块, 召回
  受限) — 非引擎问题, 如实报告, 不强行优化。
- **完整多设备/双机 PD 分离部署**: 降级为后续计划 (2026-09-14 用户决定),
  需多卡硬件验证; 当前 PD-ready 架构已满足本机调度。
- **Phase 3 优化**: kernel 融合 (RMSNorm+GEMV, QKV merge, QK_norm+RoPE) /
  TMA bulk copy / PDL / MoE grouped GEMM 调优 / 性能基线 (参考 thor-bench)。
  详见 [PHASES.md](PHASES.md) Phase 3。

## 环境

| 项 | 值 |
|---|---|
| 硬件 | Jetson AGX Thor, SM110a, 20 SM, 122 GB LPDDR5X |
| 驱动 / CUDA | 595.78 / 13.3 (nvcc 13.3.33) |
| CMake / GCC | 4.4.3 (pip) / g++-14 14.2.0 (apt, aarch64) — host+device
  全 C++23 (2026-09-19 迁移; CMake>=4.0 才映射 nvcc --std=c++23) |
| ICU | 74.2 (tokenizer NFC + 正则; 仅 C API `uregex_*` 可用,
  精简安装缺 C++ 类头 `regexpattern.h`) |
| liburing | 2.5 (PLE io_uring) |
| 模型路径 | `~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream` (只读) |
| 磁盘 | NVMe, 约 360 GB 可用 |
