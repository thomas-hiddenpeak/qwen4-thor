# STATUS.md — 当前状态快照

> 本文始终反映"现在"。已完成/已解决归档见 [DONE.md](DONE.md);
> 开发时间线见 [log/](log/README.md)。文档总索引见 [README.md](README.md)。

## 当前阶段

Phase 2 — 连续批处理 / MTP 批处理 / 性能优化 (Phase 1 已闭合 2026-09-07)
(详见 [PHASES.md](PHASES.md))

## 当前焦点 (2026-09-18): 单流 decode 性能 (FP8 投影) + serve 工业化 (推进中)

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
- Phase 1 实现:**PLE 流式层 (核心特性) 已全部完成** ✅ (ngram 哈希 /
  io_uring 读取器 / FP8→BF16 转换 / 端到端 gather, 均通过真实 checkpoint
  参数 + 真实 51.2 GB sidecar 验证)。
- Phase 1 实现:IO 层。
  - ✅ 最小 JSON 解析器 (递归下降, 含 \u 转义/数字/对象/数组/错误定位)。
  - ✅ safetensors mmap 读取器 (头解析/张量元数据/按需读字节/H2D),
    在真实模型 scale 文件上验证。
  - ✅ config.json 解析 (ModelConfig 结构体 + 不变量校验), 在真实
    config.json 上验证全部关键超参 (48 层 / hidden 2560 / MoE 512×10 /
    PLE / QSA indexer / MRoPE / MTP / NVFP4)。
  - ✅ 权重加载编排 (WeightIndex + WeightLoader): 解析 index.json
    (296347 张量 → 197 shard), 按需 mmap shard + LRU 缓存, 读取与直接
    打开 shard 逐字节一致。
  - ✅ tokenizer (GPT-2 Byte-Level BPE, ICU 74 NFC + \p{L} 正则):
    独立 `q4t_text` 库。encode 做 added-token 整体子串匹配 (与
    transformers/tokenizers 一致), decode 走 id→content。在真实
    tokenizer.json 上 57 个多样化输入 (空串/CJK/emoji/NFC 组合/特殊
    标记/长文本) 与 python `tokenizers` 库逐位一致, 0 不匹配。
  **→ IO 层 (JSON / safetensors / config / 权重 / tokenizer) 全部完成** ✅
- Phase 1 实现:量化层核心。
  - ✅ `format.h` e2m1 / UE4M3 编解码 (round-to-nearest-even, 无查表,
    `__host__ __device__`)。
  - ✅ `swizzle.h` NVFP4 scale swizzle 布局 + padding + 转换。
  - ✅ `dequant.cu` NVFP4→BF16 (W4A16)。
  - ✅ `act_quant.cu` BF16→NVFP4 运行时激活量化 (W4A4)。
  - ✅ `fp4_gemm.h` cuBLASLt 原生 W4A4 GEMM 封装。
  - ✅ `moe_weights.h/.cpp` routed-expert NVFP4 权重加载编排 (4 大 buffer
    直载 + gate/up 合并 + per-expert swizzled SF + 标量), 4 项真实
    checkpoint 测试 (packed/SF/共享 scale/GEMM 参考) 全过。
  - ✅ `moe_gemm.h/.cu` grouped MoE GEMM 调度 (routed-expert 完整 forward:
    按专家分组 gather+量化 + gate/up GEMM + SwiGLU + down GEMM + 加权
    scatter-add, input_scale 按 gate/up 与 down 分别接线)。
  - ✅ 14 项量化测试 (9 核心 + 4 MoE 加载 + 1 MoE forward), 40 项全绿。
  **→ 量化层全部完成** ✅ (格式 / swizzle / dequant / act-quant / GEMM /
  权重加载 / grouped MoE forward, 全部真实 checkpoint 验证)
- Phase 1 实现:模型层 (进行中)。
  - ✅ `hyperconnection.h/.cu` Hyper-Connection (GatedResidual) 主干残差:
    `GroupedGemmaRMSNorm` (per-branch) + mix (低秩门控 `silu(x/hc)`) +
    combine (`2*sigmoid(inject)`)。独立 `q4t_model` 库, 低秩 GEMM 复用
    `Bf16Gemm`。真实 layer-0 权重验证 mix/combine (L2 rel ~3e-3)。
  - ✅ `moe.h/.cu` MoE 完整模块 (每层 MLP): router GEMM (`x @ gate^T`
    [T,512]) + top-k kernel (按 logit 选 k=10, **在选中的 k 上 softmax**
    归一化, 非全部 E) + routed NVFP4 experts (调 `MoERoutedForward`) +
    shared expert (BF16 SwiGLU, gate/up 合并单 GEMM) + 门控组合
    `out = routed + sigmoid(x @ shared_expert_gate) * shared_down`
    (不加 residual, residual 由 HC combine 处理)。`LoadMoEExtra` 从
    checkpoint 直载 5 个 BF16 权重 (gate/shared_expert.{gate,up,down}_proj/
    shared_expert_gate)。
  - ✅ 测试 `model_moe_test.cpp`: 真实 layer-2 routed NVFP4 + BF16
    router/shared 权重, T=2 k=10, 与完整 CPU 参考 (top-k + NVFP4 dequant
    routed + BF16 shared + 门控组合) 一致, L2 rel 1.7e-3。42 项全绿。
  - ✅ `linear_attention.h/.cu` linear_attention (Gated DeltaNet SSM):
    投影 + causal conv1d (SiLU) + Gated DeltaNet 递归 (SSM state [48,128,128],
    S 放 smem) + per-head RMSNorm*silu(z) gate + out_proj。真实 layer-2 权重
    验证 (out L2 rel 7.5e-3)。
  - ✅ `full_attention.h/.cu` full_attention (QSA 稀疏注意力): GQA +
    centered RMSNorm + partial RoPE + attn gate + QSA indexer (压缩 K + MQA
    logits + block top-512) + 稀疏注意力 (online softmax)。真实 layer-3 权重
    验证 (out L2 rel 4.6e-3)。
  - ✅ `decoder_layer.h/.cu` decoder layer 组装: HC mix → attn → HC combine
    → HC mix → MoE → HC combine, 单一 workspace carve (256 字节对齐)。真实
    layer-0 两条独立路径逐位一致 (A-vs-B L2 rel 0.0)。
  - ✅ `ple_layer.h/.cu` PLE 层 forward (核心特性, 0-indexed **layer 1**,
    checkpoint `ple_layer_ids=[2]` 是 1-indexed): key/value_proj (BF16 GEMM)
    + 3× GroupedGemmaRMSNorm (与 HC 同型, per-branch) + gate
    (`sigmoid(sqrt(|dot|/√hs)·sign)`) + depthwise causal conv (kernel=4,
    dilation=ngram_size=3, SiLU) + `out = gated_value + conv_out`。真实
    layer-1 权重验证 (out L2 rel 4.1e-3)。
  - ✅ PLE 注入进 decoder layer (layer 1, `attn_hc.mix` 之前):
    `DecoderLayerForward` 新增 `ple_embeddings` 参数, `has_ple` 层先
    `trunk = hyper_input + PleLayerForward(emb, hyper_input)` 再走
    attn_hc.mix/combine (combine 用校正后 trunk)。`LoadDecoderLayer` 自动
    加载 layer 1 的 PLE 权重, workspace carve 加 PLE 区 (256 字节对齐,
    `PleLayerWorkspaceBytes` 与内部 carve 一致)。真实 layer-1 两条独立路径
    (生产 vs 手动 PLE+编排) 逐位一致 (A-vs-B L2 rel 0.0)。
  - ✅ `model_head.h/.cu` 模型头/尾: embedding lookup (token→[T,hs]) +
    主干扩展 (emb 复制成 hc 个分支 → [T,hc*hs]) + 收尾
    `hyper_connection_mixer.mix` (use_combine=False, 复用 GatedResidual) →
    [T,hs] + lm_head GEMM → [T,vocab]。真实 checkpoint 加载验证
    (vocab 248320 / hs 2560), 合成权重 forward 与 CPU 参考一致
    (logits L2 rel 3.2e-3)。
  - ✅ `model.h/.cu` 完整模型 forward 编排 (Phase 1 核心): `Model` 持有 head +
    48 个 decoder layer + PLE SSD-stream embedding + 持久 buffer。
    `ModelForward` = EmbedLookup → ExpandTrunk → 层循环 (每层可选 PLE
    gather→×weight_scale→注入) → HeadForward, trunk ping-pong, 单一 workspace
    跨层复用。`DecoderLayer::ResetState` 让 prefill 从空状态开始 (确定性)。
    真实 checkpoint 端到端验证 (head + 2 层含 PLE, T=4, logits 有限/非平凡/
    两次运行逐位一致)。
  - ✅ **全 48 层完整模型端到端验证通过**: `Q4T_MODEL_LAYERS=48` 加载全部
    84 GB 权重 (36 linear + 12 full attention + PLE + head) 无 OOM (Thor
    122 GB 统一内存), T=4 prefill forward 成功, logits 有限/非平凡/两次运行
    逐位一致。期间修复 full attention workspace 低估 bug: `AttnWs` 按
    70 KiB/token 估算, 实际 `FullAttentionForward` carve 是 105 KiB/token,
    新增 `FullAttentionWorkspaceBytes(w, T)` 精确镜像 carve, 供
    `DecoderLayerWorkspaceBytes` / `DecoderLayerForward` 使用 (之前 2 层测试
    全是 linear 层, 未触发)。
  - ✅ **decode 路径 + `generate` 命令**: `ModelDecodeStep` (T=1, 不 ResetState,
    绝对 position, PLE history 从 history 数组 EOS 填充) 与 `ModelForward`
    共享抽取出的 `RunLayers`。`q4t generate "prompt" [--max-tokens N]` =
    tokenizer encode → LoadModel → prefill → greedy argmax decode 循环
    (EOS 248044 停止) → tokenizer decode。decode-vs-prefill 自洽测试通过。
  - ✅ **修复 generate 乱码根因 (linear attention norm gate 激活)**: 对照
    transformers `Qwen4ExpTextRMSNormGated`, 其激活是
    `config.output_gate_type` (qwen4_exp = **sigmoid**), 而 C++
    `NormSiluGateKernel` 误用 `Silu(z)` (conv1d 的 `hidden_act`)。测试 CPU
    参考也自洽地用了 Silu, 故单元测试一直"通过"。改 `NormGateKernel` 用
    `Sigmoid(z)` (36/48 层受影响) + 测试参考同步。**验证**: 2 层 C++ 与
    PyTorch 参考 (transformers `Qwen4ExpTextModel`, 真实权重, PLE sidecar
    gather) logits **argmax 全匹配** (cos 0.976–0.999); 48 层 generate 输出
    通顺文本 (含 thinking 模式, 能自我纠正)。
    - 附带: 参考脚本 e4m3 解码修正 — 专家 scale 是无符号 UE4M3 (仅 0x7F
      =NaN, 0x78–0x7E = 256–448 有限值), 之前误把所有 exp=15 当 NaN 导致
      参考全 NaN; 实测 checkpoint scale 字节全部 ≤0x7E, 与 C++ 解码一致。
  - ⏳ MTP 1 层 (fc_embedding/fc_hidden + pre_fc_norm + full_attention +
    BF16 MoE + mtp_hc)。**用户决定: 等整体架构完善后再推进** (2026-09-05)。
    阻塞已解除: vLLM main (`reference/vllm`, commit 2902ca1) 含完整
    qwen4_exp 实现, `nvidia/mtp.py` (461 行) 是 MTP 权威参考 —
    `fc_embedding`/`fc_hidden` 均为 per-branch `Linear(H,H)` [2560,2560]
    (**无 10240→2560 降维**, 之前布局歧义已解开), `pre_fc_norm_hidden`
    [10240] 对展平多流 [T, hc*H] 做 GemmaRMSNorm, 主模型须输出
    pre-final-mixer 多流 [T, hc*H] 给 MTP 第一步 (scheme A), MTP 层 =
    full_attention + QSA indexer + 512 expert MoE (与主干同构)。
    checkpoint 31 个 `mtp.*` 张量形状已逐一对照 vLLM 参考确认一致。
  - ✅ **4 层参考验证 (含首个 full_attention, 2026-09-06)**: 用
    transformers 5.16.1 官方 qwen4_exp 实现 (CPU torch, 真实权重,
    NVFP4 numpy dequant, PLE sidecar gather) 跑 4 层 (layer 0/1/2
    linear + layer 3 full_attention/QSA) 参考 forward, 与 C++
    `Q4T_MODEL_LAYERS=4` dump 对比: **4/4 token argmax 匹配**, cos
    0.9948–0.9991, l2_rel 4.7e-2–1.2e-1 (NVFP4 量化噪声预期内),
    top-50 重叠 44/50。**关键**: 首个 full_attention (QSA) 层在真实
    层循环中与参考一致 (此前只有 layer 3 单独单元测试)。脚本
    `.q4t-work/ref4_logits.py` (从 ref2 泛化, 支持 linear/full 混合层),
    `q4t_tests <name>` 支持测试名过滤 (单独跑重测试)。
  - ✅ **decode 路径自洽性验证 + SSM state 改 FP32 (2026-09-06)**:
    扩展参考验证到 decode 路径 (新测试 `model_forward_dump_decode`:
    prefill + greedy decode dump, `Q4T_DECODE_SELFCHK` 自洽检查
    "prefill(T+1) 最后一行 vs prefill(T)+decode(1)")。初测 3/4 层
    cos 仅 0.41/0.53, 按层二分 (1 层纯 SSM 也 0.49) 指向
    "state 交接断裂"。逐阶段中间量 dump (trunk/HC mix/qkv_raw/
    conv/y_ssm/ssm_state) 定位后**发现是测试设计错误**: self-check
    把 decode step 0 的**输出** token 追加进 prefill5, 而 decode 实际
    输入是 prefill argmax (两个不同 token) → 两条路径处理不同 token,
    差异正常。修复 (用 decode 输入 token 构造 p5) 后: **1 层
    bit-identical (cos=1.0, 全部中间量 max_abs=0), 3 层 cos=0.9985
    (top-2 logits 差 0.031, BF16 噪声可翻转), 4 层 cos=0.9988 且
    argmax 一致**。剩余漂移来自 MoE/HC GEMM 的 batch(T=5) vs
    增量(T=4+T=1) cuBLASLt 算法选择差异 (预期数值非确定, 非 bug)。
    期间把 SSM 持久 state 从 BF16 改 **FP32** (匹配 transformers 参考
    全程 FP32 递归; 使 layer-0 SSM state 在两条路径 bit-identical;
    36 层 54→108 MiB, 可忽略)。**54 项测试全绿, 零警告**。
  - ✅ **conv1d 窗口 decode 更新 bug 修复 + 参考逐步对照 (2026-09-06)**:
    自洽检查从 1 步扩展到 N 步 (用 decode 实际喂入的 token 序列做全新
    prefill, 对照增量路径最后 N 行) 后, 3 层 4 步复现累积漂移
    (step 2 cos 0.938, step 3 argmax 翻转)。根因:
    `Conv1dUpdateStateKernel` 在 T<hist (decode, T=1) 时只写最后一列,
    窗口不左移 — [a,b,c]+d 变 [a,b,d] 而非 [b,c,d], tok1 永久滞留、
    tok3 丢失。首步 decode 不受影响 (conv 读发生在更新前), 故 1 步
    自洽检查抓不到; 第 2 步起 conv 窗口错误。修复: T<hist 时先右移
    旧窗口 (state[k]=state[k+T], k<shift) 再写新 token。修复后:
    ① 多步自洽漂移消除 (step 1 0.983→0.991, step 3 0.974→0.993);
    ② **参考逐步对照** (参考脚本加固定 token 序列模式, 喂入 C++ 的
    greedy 序列): 3 层 4 步 **4/4 argmax 全匹配**, cos 0.949–0.998
    (step 2 偏低是 batch M=8 vs 增量 M=4+M=1×4 的 cuBLASLt 算法差,
    非 bug); ③ **state 逐元素对照**: conv 4/8 token 后 C++ vs 参考
    cos 0.999998/0.999999 (对齐 cpp == ref[:, 1:]), SSM 4/8 token 后
    cos 0.999995/0.999996。对照方法论纠正: 参考 conv_states[0] 存
    最后 conv_k=4 个原始输入 (含当前 token), C++ 存 conv_k-1=3 个
    历史, 对齐须取参考后 3 列; 两路径第 5 个输入 token 必须显式对齐
    (各自 greedy 可能不同)。**54 项测试全绿, 零警告**。  - ✅ **PLE short-conv 持久状态 bug 修复 (2026-09-06, 核心特性)**:
    3 层对齐复跑 (C++ 写出 `.full_seq.txt` = prompt + decode 实际喂入
    token, 参考直接读, 彻底消除 token 错位) 后, 用逐层/逐阶段中间量
    dump (trunk_in/x/qkv_raw/qkv/y_ssm/moe_in/moe_out/out/ple_emb/
    ple_gated_n/ple_conv_out) 做 C++ batch(M=8) vs C++ incremental
    (M=4+M=1) 的**无参考**对照, 逐层定位发散引入点。结论:
    ① layer 0 全部 8 个检查点 bit-identical (linear-attn 递归 + conv
    窗口 + NVFP4 MoE 全对); ② layer 1 (PLE 层) 的 `ple_emb` (NVMe
    gather 输出) 与 `ple_gated_n` (conv 输入) bit-identical, 但
    `ple_conv_out` 发散 l2_rel=0.103 → **PLE short-conv 缺持久状态**。
    根因: 参考 `Qwen4ExpTextPLELayer._short_conv` 用
    `update_conv_state(state_idx=1)` 维护 9 元素/通道状态
    (`short_conv_state_len=(K-1)*dilation=(4-1)*3=9`), 膨胀卷积
    (K=4, dilation=3) 感受野 9, 每个 token 需前 9 个 `gated_n`;
    C++ `DepthwiseConvKernel` 无 state 参数, `src<0` 零填充 →
    decode (T=1) 只见当前 token, 丢失前序 token 的 gated_n (同
    linear-attn conv1d 窗口 bug 同类, 但此前只修了 linear-attn 侧)。
    修复: 新增 `ple_conv_state` [hc*hs,9] BF16 持久状态 (Load 分配/
    清零, Free 释放, ResetState 重置), `DepthwiseConvKernel` 在
    `src<0` 时读 `state[c, src+9]` (fresh 序列 state=0 → 等价零填充,
    prefill 路径不变), 新增 `PleConvUpdateStateKernel` 卷积后滑动窗口
    (同 `Conv1dUpdateStateKernel` 逻辑, state_len=9)。修复后 (两个
    **不同**的对照, 勿混):
    **(A) C++ 自洽** (batch prefill vs incremental decode, 均 NVFP4):
    ① `ple_conv_out` batch vs incremental **bit-identical**
    (l2_rel 0.103→0.0); ② 3 层 4 步 logits **4/4 bit-identical** (此前
    step 2 l2_rel=0.317); ③ 4 层 16 步自洽 argmax 16/16, 但 logits
    l2_rel 有 sawtooth 尖峰 (step 12 达 0.25, 下一步即恢复)。**无状态
    bug 的直接证据是增量自洽性实验 (E8)**: 两条全增量路径 (Prefill(4)+
    16×decode vs Prefill(1)+19×decode, 均 M=1 收尾, 仅 pos 1–3 的 GEMM
    形状不同) 在 pos 4–19 互差 **l2_rel mean 0.000173, 15/16 bit-
    identical** — M=1 增量递推自洽, 与分块无关, 状态处理正确。交叉
    证据 (E7 等距性): 两条 C++ 路径各自对参考 (transformers FP32) 的
    mean l2 **等距** (batch 0.1306 vs incremental 0.1309), 且互差
    (0.0643) 小于各自到参考的距离。argmax 匹配本身是必要非充分条件
    (near-tie 位置 argmax 可匹配而 logits 差 0.25)。残差分解 (E9 定根
    因): 共模 (两路径 vs 参考) 0.13 = NVFP4 量化误差 (不可消除); 差模
    (batch vs incremental) 0.064 = **MoE 路由边界敏感性** — GEMM 形状差
    (M=16 vs M=1) 在 MoE router 分数上产生 ~1e-3 微小差异, 当某位置恰好
    落在 top-10 边界附近时翻转专家选择 (E9b: layer 1 pos 14, expert
    54↔207), 产生 O(1) MoE 输出差, 经后续层传播到 logits (0.25); 非边界
    位置不受影响 (E9: pos 13/15 bit-identical)。E9c 排除 router bug:
    翻转两专家归一化权重完全相同 (0.079047, top-10 最小/边界权重), 即
    原始 router 分数在 ~1e-3 内的真近并列, 良性 MoE 行为。E10 普适性:
    16 位置仅 2 处翻转 (pos 12 L2, pos 14 L1), 最大尖峰 pos 16 (0.25)
    是非翻转位置 — 翻转是稀疏触发器, 残差是此前翻转经 conv 窗口 (4) +
    SSM 状态 (收缩) 传播的累积效应; 传播模式 (窗口内抬高、窗口外衰减)
    与状态正确携带差异一致, 强化无状态 bug 结论。E8 证明状态处理无 bug;
    差模来自 MoE 离散路由, 非 SSM/conv 状态逻辑。SSM/conv 状态差实测有
    界 (layer 0 bit-identical, layer 2 conv 0.165, 非单调增长), 与单点
    MoE 翻转经 conv 传播一致。
    **(B) C++ vs 参考** (NVFP4 vs transformers FP32, 同序列, 测不可消除的
    NVFP4 量化误差): 4 层对齐序列 **8 步 6/8、16 步 12/16 (75%) argmax
    匹配** (first-max, 与生成一致); 不匹配均为参考侧 near-tie 或中等 gap
    被 l2_rel 0.10–0.30 的 NVFP4 噪声翻转 (16 步 l2_rel 0.036–0.302,
    mean 0.131, 轻微上升属噪声经状态放大的预期行为, 非状态累积 bug),
    **非状态 bug**。
    此前 "4/4 argmax 全匹配" 是**修复前**的巧合 (bug 的误差恰好保住了
    argmax 顺序, 而 l2_rel 比修复后差 4–12 倍)。此前 "MoE/HC GEMM
    cuBLASLt 算法差" 的归因**错误** — 发散全部来自 PLE conv 缺状态,
    GEMM 形状差在此序列上实测为 0。**54 项测试全绿, 零警告**。  - ✅ **长序列 QSA 稀疏路径 (T>2048) 端到端验证 + 修复**: 用自然语言长文
    (prompt 1612 + decode 600, 越过 2048 稀疏激活点) 验证, 输出全程连贯。
    期间定位并修复 4 个稀疏路径 bug (见下)。
    - **`IndexerLogitsKernel` 共享内存越界 (illegal memory access)**:
      `__shared__ float s_blk[512]` 但 `n_groups` 最大 `kMaxBlocks=2048`,
      position 2051 (n_groups=513) 时 `s_blk[512]` 越界写 → 污染 CUDA
      context, 下一个 `cudaMalloc` (hyperconnection `d_inject`, 仅 8 字节)
      报 "illegal memory access" (非 OOM)。改 `s_blk[kMaxBlocks]`。
    - **`BuildCompressedKKernel` 组尾判断用错索引**: 原 `(t+1)%compress`
      用 batch 索引 `t`, decode 时 T=1、t=0 恒不满足 → 压缩 key 永不构建。
      改 `(pos+1)%compress` (position 语义)。
    - **`BuildCompressedKKernel` prefill 跨 block 竞态**: 组尾 block 读
      `idx_raw[g0..]` 时其他 block 可能未写完, 污染 `idx_comp` (dense 区无
      影响, 但持久缓存被稀疏区使用)。拆成 `WriteIndexRawKernel` +
      `BuildCompressedKKernel` 两个 launch, kernel 边界全局同步消除竞态。
    - **`TopkSelectKernel` 因果性**: 当前 token 所在 group 在 3/4 phase 下
      不在可见压缩 key 内, 强制追加当前 group 已发射尾部 `[g0_cur, pos]`。
    - 另: `max_len` 2048→8192 (对齐 kernel 上限 `kMaxT`, 否则 decode 在
      position 2048 崩溃, 稀疏路径不可达); `SafetensorsFile` 析构加
      `posix_fadvise(DONTNEED)` + `LoadModel` 用 `unique_ptr` 释放 mmap
      (统一内存下避免 84GB 权重映射与 GPU 权重双份占用, 参考 qwen35-thor
      "立即释放 mmap")。
    - **发现 (非 bug)**: MoE `ScatterAddKernel` 用 FP32 `atomicAdd` 累加,
      顺序非确定 → logits 末位漂移 → 贪心 argmax 在运行间可能翻转 (top-1
      接近时)。PyTorch MoE 同样非确定, 属 LLM 固有特性, 非正确性缺陷。
  **→ 模型层: 全部子模块 + decoder layer + PLE 注入 + head/tail + 完整
    forward 编排 + 全 48 层端到端验证 + decode + generate + 长序列 QSA
    稀疏路径 完成, 待 MTP + 逐 token 对 SGLang 参考验证**
- [x] 2026-09-05 `serve` 命令: OpenAI 兼容 HTTP API (独立 `q4t_server`
  静态库, POSIX socket, 零第三方依赖)。
  - 端点: `GET /healthz` → "ok"; `GET /v1/models` → 模型列表;
    `POST /v1/chat/completions` → chat 补全 (stream 与非 stream)。
  - 请求解析: 复用 `q4t_io` 的 `ParseJson`/`Json` 读 messages/max_tokens/
    stream; prompt 由 messages 的 role+content 拼接 (兼容裸 `prompt` 字段)。
  - 生成: 每请求一次 prefill (重置 per-layer 状态) + greedy argmax decode
    循环 (EOS/length 停止), 与 `generate` 命令同路径。模型有状态, 所有请求
    经 `std::mutex` 串行 (并发是 Phase 2)。
  - 流式: SSE `text/event-stream`, 首 chunk 带 role, 后续带 content delta,
    末 chunk 带 finish_reason, `data: [DONE]` 结束。非流式: 标准
    `chat.completion` JSON (id/object/created/model/choices/usage)。
  - **验证 (真实 curl)**: `/healthz` → ok; `/v1/models` → 正确列表; 非流式
    "The capital of France is" → 通顺英文 + thinking, 格式正确; 流式
    "Say hello in one word" → "Hello", SSE 格式正确。52 项测试全绿, 零警告。
  **→ Phase 1 服务层 (serve) 完成**
- [x] 2026-09-12 **多模态 3D MRoPE (已闭合)**: 视觉/视频 token 的 RoPE
  位置从"纯文本逻辑位置"升级为 transformers 5.16.1 的 3D MRoPE
  (t, h, w 三行坐标 + mrope_position_delta)。
  - **布局统一**: 持久化 `d_rope_pos[3, max_len]` 按**绝对位置**寻址
    (`rope_pos[r*max_len+p]`); 3 个 RoPE kernel (主注意力 Q/K partial
    RoPE / indexer Q/K / 压缩 key) 全部改用 `positions[t]` 索引, prefill
    (positions=t) 与 decode (positions=绝对) 共用同一张表, 修复了旧
    `[3,T]` 布局在 decode 下越界/错位的隐患。
  - **位置算法** (`BuildRopePositions`): 文本段三行=文本时钟; 视觉块
    (grid T×H×W, merge m) 占 `T*(H/m)*(W/m)` token, 每 token 坐标
    (clock+tc, clock+hc, clock+wc) t-major; 块后时钟推进
    `max(H,W)/m` (非 token 数) → 产生非零 delta;
    `delta = max(row)+1-T`。decode 文本 token 三行 = `p+delta`。
  - **MTP 一致性**: `MtpModel` 加 `d_rope_pos` 恒等表 (纯文本, 三行=
    绝对位置), 修正 `FullAttentionForward` 调用漏传 rope_pos 的编译断裂;
    `LoadFullAttention` 后补 `max_len`。
  - **验证**: 差分测试 `tools/mrope_diff_test.cpp` 与 Python 参考
    `tools/mrope_ref.py` (复刻 `get_rope_index`/`get_vision_position_ids`)
    在混合序列 (4 文本+图像 1×4×4+3 文本+视频 2×4×4+2 文本) 上 63 个
    坐标逐位一致, delta=-8, decode 规则成立, 纯文本 delta=0 退化正确;
    纯文本 prefill/decode 数学确认与改动前逐位相同 (无回归)。63 项测试
    全绿, 零警告。
- [x] 2026-09-12 **验证标准体系 (Phase 2 完成标准, 已闭合)**: 把 Phase 1
  的 "L2 噪声保真度" 验证固化为可重复的 `tools/verify/` harness
  (自包含, 不再依赖被 gitignore 的 `.q4t-work/`)。
  - **三件套**: `verify_logits.py` (主驱动: encode → C++ dump → 参考
    dump → 三判据对比, 带退出码) / `ref_dump.py` (transformers 5.16.1
    参考, 逐层 lazy dequant, 支持全 48 层, 内存峰值 ~23 GB) /
    `compare_logits.py` (三判据: 置信位置 argmax / near-tie 翻转 /
    l2_rel 噪声带; 修复了原脚本 `m_gaps[mism[worst]]` 索引 bug, 补退出码)。
  - **48 层全量基线 (OVERALL PASS)**: 79-token prompt, 完整 48 层
    (比 Phase 1 的 16 层更强): 置信位置 argmax **44/44 全对** (主判据),
    8 个翻转全部 near-tie (max gap 0.368 ≤ tau 1.657), l2_rel mean
    0.140 / max 0.280 (W4A4 噪声带)。差异纯为量化噪声, 无系统性错误。
  - **l2_rel band 与层数相关** (噪声逐层累积): 4 层 ~0.05 / 16 层
    ~0.21 / 48 层 ~0.14; band [0.10, 0.35] 按完整模型校准, 短层 smoke
    的 [C] 仅供参考, [A]/[B] 才是真信号。
  - **GPU 参考 (用户建议, 已评估)**: 合理, 但时机放 Phase 2 连续批处理
    启动时 (验证频率上来后 ROI 才体现); CPU 参考保留为 gold standard
    (确定性), GPU 参考作开发期快速回归, 分歧时以 CPU 为准。

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

**MTP 批处理 Stage 1 闭合 (2026-09-13)**: draft 模型 (1 层 full-attention)
的 KV/indexer/rope 按 max_seq 池化 + MtpForward 加 d_seq_id 多序列路径
(透传 B2a 的 FullAttentionForward 机制, 单序列 bit 不变) + 隔离测试
(4 序列×2 token 打包, l2_rel≈0.002 无跨序列污染)。66 项测试全绿零警告。
下一步: MTP 批处理 Stage 2 — 批量化 draft 循环 (per-seq 滚动 trunk) +
ragged 多序列验证前向 (per-seq checkpoint/restore) + 调度器 MTP 分支
(MTP 步是多 token 前向, 与 plain 1-token/步打包模型不同)。

**B2 连续批处理全部闭合 (2026-09-13)**: B2a 引擎 (token 级打包, 多序列
decode 一次 forward, 权重只读一次) + B2b serve 调度器 (独立调度线程合并
并发请求 decode step 成 `ModelDecodeBatchMulti` 调用)。E2E: 3 并发请求
全部语义正确, 无跨序列污染; 吞吐 15.13 → 18.03 tok/s 聚合 (1.19x, MoE
专家权重读取不共享限制线性收益)。65 项测试全绿零警告。

**B1 多序列 (Phase 2 连续批处理前置) 全部闭合 (2026-09-13)**: 状态池化
(max_seq) ✅ + seq_id 穿透 ✅ + 多序列隔离测试 ✅ + serve 多请求 E2E ✅
(修复 uint16_t 字节步长越界 + float atomicAdd 非确定两个 bug)。

**Phase 1 完成标准全部闭合 (2026-09-07)。** 已完成的层: **PLE 流式层**
✅, **IO 层** ✅, **量化层** ✅ (NVFP4 W4A4 原生 + grouped MoE), **模型
层** ✅ (48 层 forward + PLE 注入 + head/tail + generate + 长序列 QSA
稀疏路径), **serve** ✅ (OpenAI 兼容 HTTP API), **PD-ready 架构** ✅
(Paged KV + 可分离代码路径 + 阶段边界 API ModelSequence), **decode 路径
正确性** ✅ (conv1d 窗口 + PLE short-conv 持久状态两个 bug 均已修复;
batch vs incremental 残差定性为 MoE 路由边界敏感性, 非状态 bug — 见
E1–E10 实验链), **MTP 推测解码** ✅, **多模态图像输入** ✅, **PLE 工作
内存/SHA-256** ✅, **greedy 生成输出与参考一致 (L2 噪声保真度)** ✅
(置信位置 8/8 + 翻转全 near-tie + l2_rel 0.207, 无系统性错误)。
62 项测试全绿, 零警告。

Phase 2 候选 (完整多设备 PD 部署等, 见 [PHASES.md](PHASES.md)): 完整
48 层 greedy 长序列端到端、Paged KV 跨设备 P/D 分离、serve 层流式
输出等 (MTP 加速比已于 2026-09-10 实测确认 1.46x, 不再属候选)。

剩余 Phase 1 项 (按 2026-09-06 与用户确认的顺序):
1. **逐 token 对参考验证 (已闭合)**: 4 层基线 + decode 自洽性 + E1–E10
   实验链已完成。正确性结论: 无状态 bug (E8 增量自洽 0.000173 + E7
   等距 0.1306≈0.1309); batch-vs-incremental 残差 = MoE 路由边界敏感
   性 (固有特性, E9/E9c/E10); C++ vs 参考 16 步 12/16 argmax (75%),
   不匹配均为 near-tie 被 NVFP4 噪声翻转。此基线作为性能优化的守护网。
2. **prefill/decode 性能优化 (进行中)**: 基线 decode 6.2 tok/s /
   prefill 18.4 tok/s。nsys 定位: 基线 GPU 利用率仅 58.8%, CPU 开销
   1061ms (cuBLASLt 每 GEMM 重建 handle+heuristic + cudaFree 同步)。
   **阶段 A 已完成 (decode 6.2→10.5 tok/s +69%, prefill 18.4→35.5
   +93%, GPU 利用率 58.8%→95.7%)**: ① cuBLASLt handle+algo 缓存
   (`lt_cache.h/.cpp`, Bf16Gemm/Fp4Gemm 按 (M,N,K,ws) 缓存 plan,
   decode 6.2→8.8); ② 层内 scratch 改 workspace 切分 + forward 路径
   cudaMalloc/Free 全改 async (cudaFree 1213ms/11165 次 → 169ms/1533
   次, 8.8→10.5)。54 项测试全绿, 零警告。
   **阶段 B 已完成 (M=1 GEMV 专用路径, decode 10.5→12.2 tok/s +16%)**:
   ③ 新增 `gemv.h/.cu` — M=1 专用 BF16 GEMV kernel (每输出元素一个
   线程, 沿 K 维向量化读 A 行 + 广播 W 列, 累加到输出; 替代 cuBLASLt
   对 M=1 tall-skinny GEMM 的低效 nvjet/cutlass WMMA 路径)。Bf16Gemm
   在 M=1 时分流到 GEMV (M≥2 仍走 cuBLASLt)。decode 10.5→12.2 tok/s
   (1527→1290 ms/16 tok), prefill 不变 (T=5 走 GEMM)。54 项测试全绿,
   零警告。**踩坑**: warmup 原用 T=1 走 GEMV 路径 (纯 kernel, 不预热
   cuBLASLt), 导致真实 prefill (T=5, 首次 cuBLASLt 调用) 付一次性
   heuristics 开销 ~64ms (prefill 140→205ms 回归)。修复: warmup 改用
   与真实 prefill 相同的 T, 走 GEMM 预热 cuBLASLt heuristics, prefill
   回到 141ms。
   **阶段 B profile 定位 (nsys, decode 1290ms GPU busy)**: GEMV 现在是
   #1 瓶颈 (733ms, 56.8%), 但**大形状已接近带宽极限** (lm_head N=248320
   241GB/s=100% 峰值, N=12288 229GB/s=95%, N=6144 215GB/s=89%,
   N=10240 195GB/s=81%; 峰值 240.9GB/s 由 bw_probe 实测)。小形状
   (N=2560 o_proj/out_proj, N=320 HC mix_down) 仅 53%, 但绝对量小
   (125+46ms)。GEMV 整体受限于权重读取带宽 (decode 物理下限: 必须读
   全部 BF16 权重 ~12.4GB/step)。非 GEMV 剩余: nvjet NVFP4 (MoE expert
   GEMM) 149.5ms / SparseAttention 108.8ms / quant-dequant 88.7ms /
   RouterTopk 87.5ms / norm 62.1ms。CPU 开销 86.9ms (cudaMemcpyAsync
   880ms 墙钟但 GPU 重叠, cudaLaunchKernel 196.8ms/57360 次)。
   **阶段 C 已完成 (decode 流量根因分析 + RouterTopk 并行化,
   decode 12.2→12.9 tok/s +6%)**: ncu (lts__t_bytes.sum) 实测 decode
   第 1 步总流量 11.3 GB/step → 138 GB/s (57% 峰值)。GEMV (BF16)
   8.82 GB/step 理论 7.73 → 1.14× 无放大 (物理下限)。MoE W4A4 nvjet
   2.18 GB/step 理论 1.34 → 1.63× 读放大 — **证伪 split-K 假设**
   (Fp4Gemm 加 M==1 禁 split-K 后 ncu 流量完全不变, heuristic 本就选
   非 split-K 算法; 改动保留为防御性), **真正根因 = nvjet 128×128 GEMM
   tile 跑 M=1, grid 仅 12-20 blocks on 20 SMs = 7-12.5% 占用率,
   延迟受限**。RouterTopk 原 thread 0 顺序扫 512 expert (512×k 串行
   min-find 依赖链) 113.5μs×48=5.8ms/step; 改为 256 线程协作加载
   shared + k 轮并行 max 归约 (warp shuffle + 跨 warp shared,
   value-desc/id-asc tie-break 保证确定性), kernel 113.5μs→8.3μs
   (13.7×), decode 12.2→12.9 tok/s (2468→2319ms/30tok), 62 项测试
   全绿, MoE l2_rel=0.0016622 不变。
   **阶段 D 已完成 (GEMV 重写, decode 12.9→14.2 tok/s +10%)**: 参考
   qwen35-thor `gemv_kernel_scattered` 重写 `Bf16GevKernel`:
   block-per-output → **warp-per-output** (32 线程算 1 输出, 8
   warp/block); 标准 FMA → **f32x2_fma** (`fma.rn.f32x2`, SM110a
   1.97×); x 每次迭代从 L2 读 → **协作加载 SMEM 一次**; 顺序映射 →
   **散列映射** (DRAM bank); block reduce → **warp reduce**。decode
   12.9→14.2 tok/s (2319→2115ms/30tok), step span 78.8→71.9ms, 小
   shape (N=2560) 53%→85% 峰值, 62 项测试全绿, l2_rel 不变。
   **FP4 GEMV 探索 (负结果, 已回退, v1/v2/v3 三版全败)**: 为 MoE W4A4
   手写 `Fp4GevKernel` 三个版本 (v1 per-element LUT+ldexpf / v2 256 项
   product LUT SMEM / v3 16 项 e2m1 LUT 进寄存器 + 每 warp 4 输出),
   正确性均 max_rel=1.86e-07, **但全部比 nvjet 慢 1.4-2.3×** (17-27μs
   vs 11.9μs, N=1280)。nvjet tensor core 硬件 dequant 实测 240 GB/s
   = **100% DRAM 峰值**。数学证明: SM110a 发射上限 152 Ginst/s →
   跑满 240 GB/s 每字节预算 0.63 条指令, 而 W4A4 反量化每字节需
   ~5-6 条 (nibble 提取 + LUT + FMA), 超出 ~10× — 只有 tensor core
   (tcgen05.mma block-scale) 能把 dequant 藏进访存延迟。qwen35-thor
   手写 FP4 GEMV 能赢是因 W4A16 (激活 BF16 无 LUT), 不可迁移 W4A4。
   **结论: "全面手写 kernel" 的边界 = BF16 路径 (已全面手写且更优) +
   FP4 W4A4 GEMM 保留 nvjet (tensor core 领域, 手写 SIMT 数学上无法
   超越)**。
   **结论: decode 14.2 tok/s, 接近带宽下限** — GEMV 大 shape 已 81-100%
   峰值, MoE nvjet 已 100% DRAM 峰值 (240 GB/s, 实测)。剩余可优化项:
   ① 融合 glue kernel (GEMV+RMSNorm, SwiGLU+ScatterAdd, GatherQuant+
   QuantF32, 参考 qwen35-thor gemv_rmsnorm_kernel / light_ops);
   ② SparseAttention (6.4ms/step, 535μs/次) 对照 qwen35-thor
   streaming/paged attention; ③ PDL (launch gap 仅 4.4%, 收益有限)。
   同时**预留 MTP 接口** (scheme A: 主模型收尾阶段可选暴露
   pre-final-mixer 多流 [T, hc*H] 给 MTP 第一步), 避免优化后再返工。
   **阶段 E 已完成 (SparseAttention 消除 256× 冗余 dot, 2026-09-08)**:
   原实现每线程 (256 个) 都对 16 个位置做完整 256-dim dot (j 循环),
   256 线程算同样的值 = 256× 冗余; 改为每线程 1 FMA partial + warp
   reduce (5 shfl) + cross-warp shared (8 adds)。62 项测试全绿。
   **阶段 F 已完成 (GroupedRmsNorm warp-per-branch, 2026-09-08)**:
   原实现 block-per-row + 4 branch 串行 + 每 branch ~10 次
   `__syncthreads` (44 barrier), T=1 时 33μs/次 = 0.6 GB/s 纯延迟;
   改为 warp-per-branch (4 branch 并行, shfl reduce 零 barrier) +
   float4 向量化 (16B=8 bf16), blockDim 256→128。hyperconnection +
   ple_layer 两份实现同步改。33μs→6.4μs/call (5.2×), 省 2.9ms/step,
   **decode 14.0→14.6 tok/s (+4%)**, 62 项测试全绿。
   **阶段 G 已完成 (MoE SwiGLU+QuantF32 融合, 2026-09-08)**: 新增
   `SwiGLUQuantKernel` (一线程一 group, 先 silu(g)*u 再量化 NVFP4,
   group max 线程内局部), 替代 SwiGLU + QuantF32 两次 launch, 删死代码
   QuantizeFloat32ToFp4Kernel。62 项全绿, MoE l2_rel=0.0016622 不变。
   decode 14.5 tok/s (M_e=1 时 glue kernel 极小, launch 开销主导, 收益
   有限)。
   **阶段 H 已完成 (MTP 接入 generate + 接受率诊断, 2026-09-08, 负结果)**:
   把 MTP 接入 `q4t generate` (加 `--mtp`/`--mtp-k` 可选 flag, 默认关闭):
   加载 MTP (借主模型 embed/lm_head) + prefill 拿 trunk_out + decode 循环
   改用 MtpSpeculativeStep + trunk 双缓冲。plumbing 正确 (62 项全绿)。
   **但 MTP 接受率 = 0** (avg 1.00 tok/step): 诊断 (Q4T_MTP_DEBUG) 揭示
   draft 输出与输入无关且随 position 奇偶交替 (271/760), 而 main 预测
   正常且 confident (top-2 gap 3.0); MTP 生成文本退化为 "hash hash hash"
   (plain 路径连贯)。根因 = MTP 推测解码的位置对齐/验证逻辑 bug (bonus
   token 计算与 plain 首 token 不一致), 需对照 vLLM mtp.py 参考专门调试
   (独立任务)。MTP 9.9 tok/s < plain 14.6 tok/s, **默认关闭**。
   **阶段 I 已完成 (MTP draft-extend 修复, 2026-09-09, 根因闭合)**:
   对照 vLLM proposer 定位根因 = **MTP 缺 draft-extend** (MtpResetState 后
   直接单 token draft, draft 注意力看不到 prompt KV → 输出垃圾)。修复:
   新增 `MtpDraftExtend` (对 prompt 跑一遍建 draft KV[0..P-1], EAGLE shift)
   + 重写 `MtpSpeculativeStep` (新签名 b/d0/g → accepted/next_b/d0/g;
   惰性验证只喂被接受 token, 无需 snapshot/rollback; 内部 extend 用验证
   期主干重建 draft KV)。**接受率 0 → 2.62 tok/step** (k=3), draft 现与
   主模型频繁一致, 输出连贯 (非 "hash hash hash")。62 项全绿。**遗留:
   净速度仍慢 (12.1 < plain 14.6) — 验证是逐 token T=1 主前向, 无批处理
   节省; 需 ModelDecodeBatch (已有 KV 上批量 T=k+1 前向) 才能实际加速
   (~1.4× 估计)。MTP vs plain 有小分歧待查 (疑 NVFP4 near-tie 噪声)**。
   **阶段 J 已完成 (MTP 批处理验证, 2026-09-09, 追平 plain)**: 新增
   `ModelDecodeBatch` (已有 KV/SSM 上批量前向 T token, 绝对 positions,
   不重置, 返回 [T,vocab] logits + [T,hc*hs] trunk)。MtpSpeculativeStep
   逐 token 惰性验证 → 一次批量 `ModelDecodeBatch([b,d_0..d_{k-1}])`
   验证 k+1 token + 条件回滚 (hybrid SSM 不可逆: snapshot; a==k 不回滚;
   a<k restore+re-advance 接受前缀; paged KV 按位置写无需回滚)。**12.1 →
   14.7 tok/s (k=2 最优, 追平 plain 14.6)**, 接受率 3.0 (k=3), 默认 k=2,
   62 项全绿。**只追平未超越**: 每步 k 个 MTP draft 前向 (BF16 MoE +
   lm_head vocab 248320) + re-advance + snapshot 吃掉节省。**真正加速
   下一步 = 量化 MTP draft MoE (BF16→NVFP4, MoE 快 ~4×) / 消除 re-advance
   (SSM kernel 暴露中间状态)**。
   **当前 kernel 分解 (14.5-14.6 tok/s, 接近带宽下限)**: Bf16Gev 48%
   (N=10240 645 GB/s 有效带宽含 L2 复用, 已近上限) / SparseAttention
   12% (T=1 grid 仅 24 blocks, 15% 占用率) / nvjet FP4 14% (100% DRAM)
   / glue ~10% (SwiGLU+Quant 已融合)。**下一步 = MTP 批处理验证**
   (ModelDecodeBatch → MTP 实际加速) 或 (可选) glue 融合 / sparse-attn。
3. **MTP 1 层 (已完成 2026-09-07)**: 权威参考:
   `reference/vllm/vllm/models/qwen4_exp/nvidia/mtp.py`。
   scheme A 接口 (2026-09-06): `ModelPrefill` / `ModelDecodeStepSeq`
   加可选 `trunk_out` 参数暴露 pre-final-mixer 多流 `[T, hc*hs]`。
   2026-09-07 完成: MTP 权重加载 (checkpoint `mtp/` 子目录, 1 层
   full_attention decoder + fc_embedding/fc_hidden/pre_fc_norm) + draft
   forward (embed→pre_fc_norm→fc_embedding; hidden.view(T,hc,H)→
   pre_fc_norm_hidden→fc_hidden (每分支共享)→1 层 decoder layer (带
   prev_block_output 注入)→mixer.combine_and_mix 出 sample_hidden
   [T,H] + multi_hidden [T,hc*H]) + 推测解码循环 (draft k 步 + 主模型
   验证 + 接受/回退 + recurrent 状态快照/恢复)。测试 mtp_draft_forward
   + mtp_speculative_step 通过。
4. **多模态图像输入 (已完成 2026-09-07)**: 权威参考:
   transformers 5.16.1 Qwen4ExpVisionModel (27 层 ViT + 2D RoPE +
   bilinear pos_embed + spatial merge) + Qwen2VLImageProcessorPil
   (PIL 后端) + Pillow 12.3.0 `Resample.c` (BICUBIC 定点)。2026-09-07
   完成四部分:
   (a) 独立 `q4t_vision` 视觉塔 (patch_embed GEMM + bilinear pos_embed +
   27 层 block [LN→QKV→2D RoPE→双向 attention→proj→residual; LN→MLP→
   residual] + merger [LN→fc1→GELU→fc2]), 333 个 `model.visual.*`
   张量加载; 端到端测试 vision_forward: CUDA vs numpy 参考 l2_rel=
   0.0317 < 0.05 (BF16 精度范围内)。
   (b) 视觉特征注入主模型: `ModelForward` / `ModelPrefill` 加可选
   `VisionFeatures` 参数, `RunPrefill` 在 EmbedLookup 后、ExpandTrunk
   前把 image token (248056) 位置的 embedding 替换为视觉特征行 (维度
   2560 = hs, 直接替换, 镜像 vllm `_merge_multimodal_embeddings` 的
   `inputs_embeds[is_multimodal] = mm_embeds_flat`)。测试
   model_vision_inject: 计数不匹配报错 + 注入改变 logits + 确定性。
   (c) **C++ 图像 processor** (`q4t/vision/processor.h/.cpp`): stb_image
   解码 (PNG/JPEG→RGB) + smart_resize (factor=32, clamp [min,max]
   pixels) + **Pillow 12.3.0 定点 BICUBIC** (a=-0.5, PRECISION_BITS=22,
   逐位复刻 `Resample.c` 的 PrecomputeCoeffs + 两遍水平/垂直) + rescale
   (/255) + normalize ((x-0.5)/0.5) + **block-major patchify**
   (per-patch [C=3,T=2,P=16,P=16], 单帧重复 T 次)。差分测试
   vision_processor: 真实 transformers 5.16.1 processor 生成的 ground
   truth 上, 恒等图 (256x256) 与 BICUBIC 图 (140x100→320x224) **均
   逐位一致 (max_abs_diff=0)**。
   (d) **serve 层多模态接入** (`chat_server.cpp`): 解析 OpenAI
   content 数组 (text + image_url 部件, base64 data URL) → 解码图像字节
   → processor → 视觉塔 → `ExpandImageTokens` (每个 `<image>` 占位符
   展开为 `grid_h/2*grid_w/2` 个 image token) → `ModelPrefill` 注入
   视觉特征。视觉塔在 `Start` 加载 (无 `model.visual.*` 时优雅降级为
   纯文本)。端到端测试 vision_e2e: 真实 PNG → processor → 视觉塔 →
   展开 → 注入 prefill → logits (有限/非平凡/注入改变 logits/确定性)。
   **61 项测试全绿, 零警告**。
5. **PLE 工作内存 <100 MiB 验证 + PLE sidecar SHA-256 校验 (已完成
   2026-09-07)**: SHA-256 与 MODEL.md 期望值逐位一致 (51.2 GB, 49s);
   工作内存实测 75.17 MiB < 100 MiB (页池 32 + staging 20 + GPU scratch
   20 + row-ids 1 + ring + reader scratch), 无 OOM 无 swap (gather 前后
   SwapFree 不变)。测试 ple_working_memory_under_100mib。**62 项测试全绿,
   零警告**。
6. **greedy 生成输出与参考实现一致 (已闭合 2026-09-07, L2 噪声保真度
   验证)**: 这是 Phase 1 最后一个完成标准。验证标准 (2026-09-06 与用户
   确认): C++ NVFP4 W4A4 引擎与 transformers 5.16.1 参考 (dequantized
   FP32 权重 + 全精度激活) 在**同一 256-token prompt** 上跑 prefill,
   比较逐位置 logits。两侧差异**就是** NVFP4 量化噪声 (C++ 把激活也量化
   到 e2m1 4-bit, 见 `GatherQuantKernel`), 所以判据问的是"差异是否只有
   量化噪声、有无系统性 bug", 而非"logits 是否逐位一致" (永远不可能)。
   - **方法**: 16 层参考 dump (`.q4t-work/ref4_logits.py` 改**逐层 lazy
     dequant** — 占位符替换 experts 参数, forward 时按需 dequant 单层
     用完即释放, 内存峰值从 ~80GB 降到 ~23GB, 否则 16 层 OOM) + C++ 16
     层 prefill dump (`model_forward_dump_decode` n_decode=0)。
   - **结果 (`.q4t-work/l2_compare.py`)**: **OVERALL PASS**。
     - **[A] 置信位置 argmax 8/8 全对** (黄金标准): 参考实现 top1 领先
       top2 超过 τ=3σ 噪声水平的 8 个位置, C++ 全部匹配。系统性 bug
       (错权重/GEMM/路由) 会破坏这些位置, 纯噪声不会。
     - **[B] 108 个 argmax 翻转全部是 near-tie** (gap ≤ τ): 参考实现自身
       就在噪声水平内, C++ 选不同 token 是预期, 非错误。
     - **[C] l2_rel 均值 0.2069** (与 W4A4 e2m1 网格 ~20% 逐元素相对误差
       理论值吻合), max 0.6539 < 0.75。
   - **解读**: raw argmax 仅 57.8% 匹配, 但**参考实现 96.9% 的位置是
     near-tie** (top1-top2 gap < 噪声), 这些位置选哪个 token 都在噪声
     内, 正确引擎靠运气匹配 ~50% + 全部置信位置。诊断
     (`.q4t-work/l2_diagnose.py`) 确认: ref_norm 不小 (均值 540, 非平坦
     logits 放大), diff_norm 均值 109 (≈20% ref_norm), pearson 均值
     0.972 (形状保留), 置信位置 top5_jacc 0.875。**结论: C++ NVFP4 引擎
     与参考实现一致, 差异纯为量化噪声, 无系统性错误。**
   **→ Phase 1 完成标准全部闭合。**

## 环境

| 项 | 值 |
|---|---|
| 硬件 | Jetson AGX Thor, SM110a, 20 SM, 122 GB LPDDR5X |
| 驱动 / CUDA | 595.78 / 13.3 (nvcc 13.3.33) |
| CMake / GCC | 3.28.3 / 13.3.0 (aarch64) |
| ICU | 74.2 (tokenizer NFC + 正则; 仅 C API `uregex_*` 可用,
  精简安装缺 C++ 类头 `regexpattern.h`) |
| liburing | 2.5 (PLE io_uring) |
| 模型路径 | `~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream` (只读) |
| 磁盘 | NVMe, 约 360 GB 可用 |
