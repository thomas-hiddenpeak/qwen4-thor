> 历史快照，保留治理前原文，不是当前状态或执行规范。
> 其中测量、结论与旧流程未经新 E2E 标准重新确认。
> 当前规则见 [EVALUATION.md](EVALUATION.md)，状态见 [STATUS.md](STATUS.md)。

# DATAFLOW_OPTIMIZATION.md — 数据流驱动的逐级优化分析

> **定位**: 本文是"沿数据流动方向逐级优化"的**分析依据**, 不是实施记录。
> 实施进度见 [STATUS.md](STATUS.md) / [log/](log/README.md)。
> **方法**: 先整理整个推理过程的数据流, 再逐环节用 CUDA/PTX 视角
> (寄存器/缓存/SM/tensor core/指令开销) 分析, 分次序、分级别处理。
> **代码是最高事实来源**: 本文所有数据流与账单基于 2026-09-20 的源码
> (`decoder_layer.cu` / `hyperconnection.cu` / `linear_attention.cu` /
> `full_attention.cu` / `moe.cu` / `moe_gemm.cu` / `ple_layer.cu`),
> 实现变化时以代码为准并修正本文。
> **所有 GB 数字是估算**, 实施前必须插桩实测确认 (项目"先测量再决定"原则)。

## 0.0 项目北极星 (2026-09-20 收敛)

**一句话**: 一台针对固定模型、有持久状态的数据流执行机。对外是 runner 接口,
对内把计算依赖、权重布局、状态更新、执行计划共同构造成一台机器。优化单位是
**一次完整的状态转换** (prefill 分块 / decode 批处理 / MTP 含回滚)。静态的是
机器结构 (模型包), 动态的是流经的数据。机器贴**两根屋顶**: 内存侧最小化必须
搬运的字节, 算力侧手写 tensor core 到 >50%。模型包显式声明**数值契约**
(精度/舍入/归约/路由/状态提交), 验证查对契约的符合性。host 退出逐层指挥,
只做接入/资源/输出/PLE 存储层协同。

### 0.0.1 八条原则 (用户原话, 2026-09-20)

1. 可以接受完全没有兼容性, runner 为 qwen4 架构设计, 新项目可作脚手架延续。
2. 可以接受逐渐完成手写 kernel, 总方向不变。
3. 系统目标完全可以在模型整个可用上下文阶段实现不同路径的 kernel 编写。
4. 不要怕麻烦, 如同地毯扫描一样解决。
5. runner 的意义就是定制, 完全沿着模型推理的数据流向引导数据流向硬件。
6. 无法超过榨干算力的锁定硬件和模型的极度专用 runner 本身没有意义。
7. 本轮优化坚决不涉及量化和 MTP。
8. 一定要用 API E2E 方式测试, 可在 tools 中创建 uv 环境用 evalscope 做吞吐
   评估。

### 0.0.2 四条补充

1. **脚手架 = 数据流机器的参考设计蓝图和最佳实践** (非未来模型兼容)。
2. **MoE 流量估计以实际激活位置为准** (非总加载权重): decode T=1 只读 top-10
   专家 ≈12GB/forward; prefill T=8192 近全专家激活 ≈70GB/forward。
3. **库在 NVFP4 上 <10% 硬件能力, 手写 tensor core >50%** (用户: "就按照超过
   50%算吧, 别纠结70%了, 我在其他项目中实现了")。
4. **项目由 AI 编写, 不在乎工作量, 第一性原理, 死磕**。

### 0.0.3 两极框架 (取代"先带宽后算力")

机器贴两根屋顶, 优化优先级按**哪根屋顶更高**排, 不是按"哪个最便宜"排:

- **Pole 1 (内存屋顶)**: 最小化必须搬运的字节。prefill 激活搬运 ~347GB/forward
  (本文 §2-§4 估算), 手写可省 ~82.8GB (24%)。
- **Pole 2 (算力屋顶)**: 手写 tensor core GEMM 到 >50% 峰值。库 (cuBLASLt/
  nvjet) 在 NVFP4 上 <10% 峰值, 手写 tcgen05.mma 在 thor-bench 实测 595
  TFLOPS = 57.5%。**实测 (gemm_roofline_bench, 2026-09-20)**: 库在我们 BF16 shape 上
  T=8192 达 **46-72% 峰值** (GDN in_proj_qkv 61% / QSA qg 72% / HC inject
  62%), **不是 <10%** — "<10%" 是 NVFP4 的事, BF16 库好得多。真正的库低效
  点在**瘦 GEMM**: HC down (N=320) 仅 **15.5%**、HC up **28.8%**。所以
  Pole 2 在 BF16 大 GEMM 上**没那么大** (库已 61-72%, 手写难超越), 但在
  瘦 GEMM 上**很大** (15.5% → 融合/手写可 3×)。详见 §4.4 实测。

**关键判据修正**: 此前"已证手写打不过 nvjet"只证了 **SIMT 打不过 tensor core
库**, 不是 tensor core 手写打不过库。正确对比是手写 tcgen05.mma vs 库:
NVFP4 上手写赢 5-6×。

### 0.0.4 三段式组织

| 段 | 职责 | 静态/动态 |
|---|---|---|
| **模型构建器** | 声明数值契约 (精度/舍入/归约/路由/状态提交), 生成机器结构 (权重布局/依赖图/执行计划) | 静态 (模型包) |
| **设备端执行机** | 一次完整状态转换的执行: prefill 分块 / decode 批 / MTP 含回滚, 贴两极屋顶 | 动态 (数据流) |
| **host 服务层** | 接入/资源/输出/PLE 存储层协同, 退出逐层指挥 | 薄 |

**数值契约要求**: 每个 kernel 显式声明输入/输出精度、舍入模式、归约顺序、
路由边界、状态提交点。验证 (tools/verify 48 层参考 + 三判据) 查的是对契约的
符合性, 不是 bit-exact (除非契约声明 bit-exact)。

### 0.0.5 对外部"LLM 数据流机器"提案的 5 处修正

1. 状态转换公式 (S_{t+1}, L_t) = F_W(S_t, x_t) 漏了 prefill/批处理 (我们主
   战场) 和 MTP 状态快照/回滚。
2. PLE 分类过粗 ("host I/O") — 应是计算-存储协同 (ngram ID 可预测)。
3. 漏了上下文阶段维度 (N 个执行计划, 非 2 个)。
4. 漏了算力屋顶 (Pole 2)。
5. 优化单位应是"一次完整状态转换", 不是"一个 kernel"。

## 0. 方法论

### 0.1 核心哲学 (用户提出, 2026-09-20)

作为专用模型在专用设备上的 runner, 不必延续参考项目的通用件思路,
可以**按权重与数据的流向, 针对全部可用硬件 (寄存器/高速缓存/SM/
tensor core/CUDA) 编写每个环节的 kernel**。约束是带宽、容量、指令开销,
且是**逐层逐级**的。

### 0.2 分级框架 (用户提出)

面对不同瓶颈, 用不同手段, **分次序、分级别**:

| 级别 | 触发条件 | 手段 | 典型收益 |
|---|---|---|---|
| **L1 带宽** | 带宽挑战 | 消除无效搬运 (读出但没贡献输出的中间激活) | 大 (prefill 激活 ~347GB, 见 §2.3) |
| **L2 计算** | 算力瓶颈 | 降低复杂度 (近似/稀疏/分层) | 中 (精度-复杂度权衡) |
| **L3 融合** | 局部都优化后 | kernel 融合 / 执行模型 (CUDA Graph/PDL) | 最小 (~1.2-1.4×) |

**次序: L1 → L2 → L3。** 无效搬运若是大头, 收益比 launch 开销高一个
量级, 应先吃。L3 (CUDA Graph) 收益最小, 放最后。

### 0.3 分场景 (决定性)

同一环节在 decode 与 prefill 下性质**完全不同**:

| 场景 | 激活大小 | 瓶颈 | 优化价值 |
|---|---|---|---|
| **decode T=1** | `[1,·]` 极小 (KB 级) | 有效权重读取的物理带宽地板 (241 GB/s, 已闭合) | **无** (激活 <0.3%) |
| **prefill T=8192** | `[8192,·]` 大 (GB 级) | 激活搬运主导 (激活:权重 ≈ 15-20×) | **大** (主战场) |

**结论: 本文所有优化都针对 prefill/批处理 (降 TTFT), decode 已闭合。**
用户真实场景 = agent 开发 (40K-200K prompt = 大 prefill), 正对此主战场。

### 0.4 硬件视角 (四维度)

每个环节按四个维度分析现状 vs 手写最优:
1. **寄存器**: 每线程驻留多少数据, 是否 spill, ILP 如何。
2. **缓存**: 张量在 HBM/L2/寄存器间流转几次, 有无冗余读写。
3. **SM**: grid/block 配置, 占用率, 并行度是否填满 20 SM。
4. **tensor core**: GEMM 是否算力受限 (tensor core 正确) 还是带宽受限
   (瘦 GEMM, 手写 SIMT 融合可省搬运)。

**关键判据 (两极框架, 见 §0.0.3)**: GEMM 若**算力受限** (prefill 大 M),
方向是**手写 tensor core** (tcgen05.mma) 到 >50% 峰值 — 库在 NVFP4 上 <10%
峰值, 手写赢 5-6×; 我们 BF16 shape 库利用率待测 (gemm_roofline_bench)。若
**带宽受限** (瘦维度), 手写 SIMT 融合可省搬运。elementwise/recurrent 链
(conv/norm/gate/rope/combine) 是手写主战场。

## 1. 核心结构发现: 窄流计算, 宽流残差

这是整个数据流里最关键的、也是手写 CUDA/PTX 能利用的结构特征:

- **计算宽度 (窄流) = `[T, 2560]`**: attention 和 MoE 真正做 GEMM 的维度
  (hs=2560)。
- **残差宽度 (宽流) = `[T, 10240]`** = 4 分支 × 2560 (Hyper-Connection
  主干, hc_count=4)。
- 宽流是窄流的 **4 倍**。每层 attention/MoE 只在窄流上算, 但宽流在
  `mix`/`combine` 处被反复读写 (每层 4-6 次)。
- 宽↔窄的桥梁 (HC mix/combine) 是**低秩 (320)** 投影: 计算便宜, 但要
  搬运整个 `[T, 10240]` 张量。

**这个 4× 的宽窄比, 就是"无效搬运"的结构性来源, 也是逐环节优化的靶心。**

## 2. 完整数据流

### 2.1 全局 (每序列)

```
input_ids [T]
→ embed_tokens [T, 2560]
→ 复制 ×4 → trunk [T, 10240]          ← 宽流诞生
→ 48 × DecoderLayer (见 2.2)
→ hyper_connection_mixer: [T,10240] → [T,2560]
→ final RMSNorm
→ lm_head [2560 → 248320] → logits [T, 248320]
→ sample → next token
```

### 2.2 单层 DecoderLayer (基于 `DecoderLayerForward` 真实顺序)

```
IN:  trunk [T, 10240]  (宽流, 上一层输出)
     ple_embeddings [T, 2560]  (仅 layer 2)

1. [PLE, 仅 layer 2]  trunk += PLE(ple_embeddings, trunk)   → [T,10240]
2. attn_hc.mix(trunk) → mixed [T,2560] (窄), res_a [T,10240] (宽)
3. attention(mixed) → block [T,2560]
   ┌ FULL (12 层, layer 3,7,...,47): QSA indexer + sparse GQA + o_proj
   └ LINEAR (36 层): GDN in_proj → conv1d → SSM scan → NormGate → out_proj
4. attn_hc.combine(block, trunk, res_a) → combined [T,10240] (宽)
5. mlp_hc.mix(combined) → mixed_mlp [T,2560] (窄), res_m [T,10240] (宽)
6. MoE(mixed_mlp) → block [T,2560]   (512 专家 top-10 + shared)
7. mlp_hc.combine(block, combined, res_m) → out [T,10240] (宽)

OUT: out [T, 10240]  (宽流, 给下一层)
```

### 2.3 每环节搬运量总表 (prefill T=8192, `[T,10240]`=160MB)

| 环节 | 层数 | 激活/forward | 权重/forward | 激活:权重 |
|---|---|---|---|---|
| HC mix/combine | 48×2 对 | **153.6GB** (960×[T,10240]) | ~10GB | 15× |
| GDN 链 | 36 | **72GB** (2.01GB/层) | 4.2GB | 17× |
| MoE | 48 | **94.5GB** (1.97GB/层) | 6GB | 15.6× |
| QSA full attn | 12 | **24.2GB** (2.02GB/层) | 1.5GB | 16× |
| PLE | 1 | **2.62GB** | 0.13GB | 20.8× |
| **合计** | | **≈347GB/forward** | ~22GB | |

**权重读取基线 (按实际激活, 非总加载, 见 §0.0.2)**: decode T=1 只读
top-10 专家 ≈12GB/forward; prefill T=8192 近全专家激活 ≈70GB/forward。
prefill 总激活搬运 (≈347GB) 是权重读取 (~70GB) 的 ~5× — 真实瓶颈结构:
**prefill 搬激活 (Pole 1), decode 读权重 (已闭合)**。

## 3. 五环节分析

> 每环节: 数据流 → 搬运账单 → 四维度现状 → 手写机会 → 量化预期。
> "可省"指 L1 (消除无效搬运) 的估算收益, 实施前需插桩确认。

### 3.1 HC mix/combine (宽↔窄桥) — P0

**数据流** (每层 2 对: attn_hc + mlp_hc):

```
mix (5 步):
  1. GroupedRmsNorm:  读 trunk[T,10240] → normed[T,10240]   (手写, warp/branch)
  2. down GEMM:       读 normed → down[T,320]               (cuBLASLt, K=10240)
  3. SiluDiv:         down 原地 silu(x/4)                    (手写, 极小)
  4. up GEMM:         读 down → up[T,10240]                  (cuBLASLt, K=320)
  5. MixGate:         读 up+normed → mixed[T,2560]           (手写, 4:1 归约)
combine (3 步):
  1. inject GEMM:     读 normed → inject[T,4]                (cuBLASLt, N=4)
  2. ApplyInjectGate: inject 原地 2·sigmoid(x/4)             (手写, 极小)
  3. CombineWithGate: 读 R[T,10240]+block_output[T,2560]×4+inject → out[T,10240]
```

**搬运账单 (每对 mix+combine, `[T,10240]` 等价单位)**:

| 张量 | 写 | 读 | 小计 |
|---|---|---|---|
| normed [T,10240] | 1 | 3 (down GEMM, MixGate, inject GEMM) | 4 |
| up [T,10240] | 1 | 1 (MixGate) | 2 |
| trunk/hyper_input [T,10240] | — | 2 (rmsnorm, Combine 的 R) | 2 |
| out [T,10240] | 1 | — | 1 |
| block_output [T,2560]×4 冗余 | — | 4 (Combine 每分支读一次) | 1 (等价) |
| **每对合计** | | | **10×[T,10240]** |

每层 2 对 = 20×, 每 forward 48 层 = **960×[T,10240] = 153.6GB**。

**四维度现状**:
- **寄存器**: GroupedRmsNorm 已优化 (warp/branch, float4 向量化, 2-pass,
  行驻 L1, 零 spill)。
- **缓存**: normed 写 1 读 3, up 写 1 读 1, 全落 HBM (T=8192 时 160MB > L2)。
  block_output 在 Combine 被 4 分支各读一次 (纯冗余)。**normed 4 次触碰 +
  block_output 4× 冗余 = 主要浪费。**
- **SM**: down GEMM (N=320, K=10240) 与 inject GEMM (N=4, K=10240) 都是
  "读 [T,10240] 归约到极小输出" → **带宽受限, SM 算力空闲**。
- **tensor core**: 这 3 个 GEMM 是瘦维度 (带宽受限), tensor core 利用率低;
  手写 SIMT 融合能省搬运 (不是打不过 tensor core, 是 tensor core 帮不上)。

**手写机会**:

| 融合 | 做法 | 节省 | 风险 | bit-exact |
|---|---|---|---|---|
| **HC-A** mix 前端合一 | 1 kernel: trunk 行 [1,10240]=20KB 载入 smem, 算 rmsnorm, 直接从 smem 做 down(320 点积)+inject(4 点积) 两个归约。normed 仍写回 (给 MixGate)。省 normed 的 2 次重读 | **30.7GB** (2×[T,10240]/对 × 96) | 中 (运算顺序变) | 否 (噪声验证) |
| **HC-B** combine 去冗余 | 每 warp 管 1 个 (t,c 段) 的全部 4 分支, block_output 值驻寄存器一次, 4 分支复用。省 3×[T,2560]=0.75×[T,10240]/对 | **11.5GB** | 低 (只改读序) | 大概率是 |

**up GEMM** ([T,320]×[320,10240] 真 GEMM, prefill 算力受限) — 两极框架下
方向是手写 tensor core (库 <10% 峰值), 但本轮先测 BF16 库利用率再定, 暂不
动。

**结论**: HC 是手写第一靶点 — 纯 SIMT 友好 (3 个瘦 GEMM 带宽受限), prefill
无效搬运最大单一来源 (153.6GB), 可省 42.2GB (27%)。**先 B (低风险、
bit-exact) 验证方法论, 再 A (高收益、需噪声验证)。**

### 3.2 GDN/DeltaNet (36 层 linear attention) — P0

**数据流** (基于 `LinearAttentionForward`, nkh=16, nv=48, kd=vd=128,
in_qkv=10240, v_dim=6144):

```
IN:  x [T, 2560]  (窄流)
1. 4 个 in_proj GEMM (全读 x):
   in_proj_qkv: → qkv_raw [T,10240]   (cuBLASLt, FP8 shadow)
   in_proj_z:   → z       [T,6144]    (cuBLASLt, FP8 shadow)
   in_proj_a:   → a       [T,48]      (cuBLASLt)
   in_proj_b:   → beta    [T,48]      (cuBLASLt)
2. CausalConv1dWithCkpt: 读 qkv_raw → qkv [T,10240]
   Conv1dUpdateState:    再读 qkv_raw → 更新 conv_state
3. GdnRegPrepNorm: qkv 原地 q/k L2-norm (读+写)
   GatedDeltaNetReg: 读 qkv+a+beta+ssm_state(O(1)) → y_ssm [T,6144]
4. NormGate: 读 y_ssm+z → y_ssm 原地 (per-head RMSNorm × sigmoid(z))
5. out_proj GEMM: 读 y_ssm → out [T,2560]
OUT: out [T, 2560]
```

**搬运账单 (每 token)**:

| 张量 | 大小 | 触碰 | 小计 |
|---|---|---|---|
| x [T,2560] | 5KB | 读 4×(4 GEMM) | 20KB |
| qkv_raw [T,10240] | 20KB | 写 1 + 读 2(conv, state) | 60KB |
| qkv [T,10240] | 20KB | 写 1 + 读写 1(prep-norm) + 读 1(GDN) | 100KB |
| z [T,6144] | 12KB | 写 1 + 读 1 | 24KB |
| a/beta [T,48]×2 | 0.2KB | 各 1w+1r | 0.4KB |
| y_ssm [T,6144] | 12KB | 写 1 + 读写 1(NormGate) | 36KB |
| out [T,2560] | 5KB | 写 1 | 5KB |
| **合计** | | | **~245KB/token** |

每层 2.01GB (T=8192), 36 层 = **72GB/forward**。

**四维度现状**:
- **寄存器**: GDN recurrence 已是 register-state (warp-per-vd-column,
  ROWS=8, state 驻寄存器, 零 spill) — **手写哲学已在此落地, 不动**。
- **缓存**: qkv 家族 (qkv_raw+qkv) 占 140KB/token = 总激活 57%, 是最大
  冗余源。conv 与 state-update 两 kernel 读同一 qkv_raw (纯冗余)。
- **SM**: conv grid(40,T) 并行充足; GDN reg grid(4,48)=192 blocks 合理;
  **NormGate grid(T,nv) 每 block 只动 256B — launch/延迟受限**。
- **tensor core**: in_proj/out_proj 是真 GEMM (prefill 算力受限),
  **cuBLASLt/nvjet 正确, 保留**。GDN recurrence 是 rank-1 更新, 非 GEMM。

**手写机会** (只动 elementwise/recurrent 链, GEMM 保留):

| 融合 | 做法 | 节省 | 风险 | bit-exact |
|---|---|---|---|---|
| **① prep-norm→conv** | conv kernel grid(ch=40,T), 每 block 256 通道恰覆盖 2 个 q/k 头, 算完 SiLU(conv) 后 block 内直接做 2 个 128 维 RMSNorm (warp shuffle), prep-norm kernel 消失 | **11.5GB** (40KB/token) | 低 | 大概率是 |
| **② NormGate→GDN** | GDN reg kernel 的 warp 拥有 ROWS=8 列 × 全 T 的 y (寄存器), 同 value head 的 16 warp 经 smem 跨 warp 归约 + barrier, 各 warp 用自己 8 列 × norm × sigmoid(z) 写 y_ssm | **10.4GB** (36KB/token) + 省 393K launch | 中 (barrier 影响 scan ILP) | 否 (噪声验证) |
| **③ conv+state 合并** | conv kernel 算完顺手把本 token raw 值写 conv_state 滑窗 (k=4), state-update kernel 消失 | **5.8GB** (20KB/token) | 低 | 是 |
| **④ 4 in_proj 合并** | 加载时拼权重 [2560,16480], 1 GEMM 输出 [T,16480], slice 出 qkv_raw/z/a/beta (x 只读 1 次) | **4.3GB** (15KB/token) | 低 | 是 |

**结论**: GDN 是手写第二靶点, **比 HC 更容易下手** (主要是 elementwise
kernel 合并, 不涉及低秩 GEMM 寄存器压力), 可省 32GB (44%, 比例最高)。
**先做 ①+③+④ (低风险、bit-exact、21.6GB), 验证后再做 ② (需噪声验证)。**

### 3.3 QSA indexer + sparse attention (12 层 full attention) — P2

**数据流** (基于 `FullAttentionForward`, nq=24, nkv=2, hd=256, rot_d=64,
idx_hd=128, n_iq=4, n_ik=1, compress=4, block_topk=512):

```
IN:  x [T, 2560]
1. 5 个 in_proj GEMM (全读 x):
   qg=x@W_q [T,12288] (FP8) | k=x@W_k [T,512] (FP8) | v=x@W_v [T,512] (FP8)
   iq=x@W_iq [T,512] | ik=x@W_ik [T,128]
2. QKDeinterleaveNorm: qg → q[T,6144]+gate[T,6144]+norm(k)
3. PartialRope: q(24)+k(2) 3D MRoPE in-place
4. WriteKV: k,v → paged KV cache (持久)
5. ik_raw = D2D copy(ik)
6. IndexerNormRope: iq+ik in-place
7. WriteIndexRaw: ik_raw → idx_raw (持久)
8. BuildCompressedK: idx_raw → idx_comp (group avg, compress=4, 持久)
9. 打分+top-k (3 路径):
   短(≤8192): Bf16Gemm S=iq@ck^T → IndexerReduce → TopkSelect
   长 decode(T≤4): OnePassScore(warp/block) → 多级 Topk
   长 prefill: 流式 CHUNK=2048 (Gemm+Reduce+MergeChunkTopk)
10. SparseAttention: q × topk-KV (tensor core mma.sync, grid(T,nkv=2))
11. out = attn @ W_o^T: [T,6144] → [T,2560] (FP8)
OUT: out [T, 2560]
```

**搬运账单 (每 token)**: ~247KB (x 5× 读 25KB + qg [T,12288] 48KB +
q 36KB + gate 24KB + S/logits/topk ~64KB + attn 24KB + 其他 ~26KB)。
每层 2.02GB (T=8192), 12 层 = **24.2GB/forward**。

**四维度现状**:
- **寄存器**: SparseAttention mma.sync fragment, q/k/v 驻寄存器, 每 block
  读 KV chunk 一次喂 12 q-heads (**12× KV 读削减**) — 已高度优化。
- **缓存**: **x 被 5 GEMM 各读 1 次** (25KB/token)。qg [T,12288] 写 1 读 1
  (24KB/token, 最大中间张量)。
- **SM**: SparseAttention grid(T,2) T=8192 → 16384 blocks, 充分并行。
- **tensor core**: 5 in_proj + indexer GEMM + SparseAttention **全部已最优**
  (nvjet/mma.sync), 手写 SIMT 打不过 (已证)。

**手写机会** (只动 elementwise 链):

| 融合 | 做法 | 节省 | 风险 | bit-exact |
|---|---|---|---|---|
| **① 5 GEMM 合并** | 拼权重 [2560,14000], 1 GEMM 输出 [T,14000], slice (x 只读 1 次) | **1.9GB** (20KB/token) | 低 | 是 |
| **② deinterleave+rope 合一** | 1 kernel grid(T,nq+nkv), 每 block 1 head 256 维: 读 qg → deinterleave → RMSNorm → RoPE → 一次写出 (省 q 中间读写) | **1.15GB** (12KB/token) | 低 | 大概率是 |
| ③ IndexerNormRope+WriteIndexRaw | **不可行** (WriteIndexRaw 读 pre-norm 的 ik_raw, 依赖 D2D copy, 数据依赖) | — | — | — |

**结论**: QSA 是 5 环节里手写收益最小 — tensor core 路径全最优, 可省只
3.05GB (13%)。**prefill #1 瓶颈 (28.7%) 是 SparseAttention 的 tensor core
计算, 不是带宽, 手写帮不上**; 进一步降 TTFT 需算法级 (降 top-k 候选块/
近似打分), 属 L2 (精度-复杂度权衡, 需实验, 用户要保召回)。

### 3.4 MoE (512 专家 top-10 + shared) — P2

**数据流** (基于 `MoEForward` + `MoERoutedForward`, hs=2560, E=512, k=10,
moe_is=640, shared_is=640):

```
IN:  x [T, 2560]
1. router GEMM: x @ gate^T → logits [T,512]   (Bf16Gemm, 读 x)
2. RouterTopk: logits → eid[T,10]+rw[T,10]    (手写, smem 512, 串行 top-10)
3. MoERoutedForward (NVFP4, 4 stream round-robin):
   BuildTokenLists (GPU) → counts D2H (结构性同步 0.23%, 已否决 GPU-resident)
   → host 建 offset/row_of_flat → H2D
   → per-expert: GatherQuant(读 x 行) → gu GEMM(nvjet FP4) → SwiGLUQuant
                 → dn GEMM(nvjet FP4)
   → combine: gather → d_routed [T,2560] float (确定性)
   注意: R = T×k = 10T 行 (每 token 展开 10 份)
4. shared_gu GEMM: x → [T,1280] (FP8, 读 x)
5. SwiGLU: [T,1280] → [T,640]
6. shared_down GEMM: [T,640] → [T,2560] (FP8)
7. MoECombine: d_routed + shared_down + x*scalar → y [T,2560]  (读 x)
OUT: y [T, 2560]
```

**搬运账单 (每 token)**: ~240KB (x 13× 读 65KB [router 1+gather 10+shared
1+combine 1] + d_routed float 20KB + R=10T scratch 128KB + 其他 27KB)。
每层 1.97GB (T=8192), 48 层 = **94.5GB/forward** (5 环节最大)。

**四维度现状**:
- **寄存器**: RouterTopk smem 512, 串行 top-10 (10 轮 block-reduce+mask),
  T=8192 时 8192 blocks 并行, 非瓶颈。
- **缓存**: **x 被读 13 次** (65KB/token, 占激活 27%)。**R=10T 行 scratch**
  (a_packed/gu_out/dn_out) 是 T 的 10 倍展开 (128KB/token, 占 53%)。
- **SM**: per-expert 4 stream round-robin, 小 M_e (几十 token) 多 stream
  重叠填 SM。
- **tensor core**: gu/dn GEMM nvjet FP4 — **已证手写 SIMT 打不过 (1.4-2.3×
  慢)**。router/shared GEMM cuBLASLt 正确。

**手写机会**:

| 融合 | 做法 | 节省 | 风险 | 结论 |
|---|---|---|---|---|
| **① d_routed 改 BF16** | d_routed 从 float 改 BF16 (10→5KB/token), combine 时转 float 累加 | **3.8GB** | 低 | 做 (格式优化) |
| **② persistent token-parallel** | 每 block 处理 1 token 全部 10 专家, x 行读 1 次进 smem 共享。省 9×5KB=45KB/token | 17.3GB | **极高** | **否决** (重写调度模型, 可能失去 nvjet → 净损失, 架构级非 kernel 融合) |

**结论**: MoE 手写收益最受限 — GEMM 全最优, 最大冗余 (x 10× gather) 需
persistent 重构且可能失去 nvjet (否决)。**94.5GB 激活里 96% 是"有效"的**
(R=10T 展开是 MoE 算法本质, x 10× gather 是 per-expert 必需) — 印证
"无效搬运"思路在 MoE 上收益有限。只做 ① (3.8GB)。

### 3.5 PLE (查表 + 门控 + conv, 51.2GB NVMe 表) — P3

**数据流** (基于 `PleLayerForward`, 仅 layer 2, hc=4, hs=2560, pe=2560,
hc_dim=10240, K=4, dil=2, state_len=6):

```
IN:  embeddings [T, 2560]  (NVMe 51.2GB 表, 16 row_id × 160B)
     hyper_input [T, 10240]  (宽流 trunk)
1. key = embeddings @ key_proj^T:  [T,2560] → [T,10240]  (Bf16Gemm)
2. value = embeddings @ value_proj^T: [T,2560] → [T,2560] (Bf16Gemm)
3. key_n = GroupedRMSNorm(key):  [T,10240] → [T,10240]
4. query_n = GroupedRMSNorm(hyper_input): [T,10240] → [T,10240]
5. gate = (key_n·query_n).sum(-1)/sqrt(2560) → sigmoid·sqrt: [T,4]
6. gated = gate × value:  [T,4]×[T,2560] → [T,10240]
7. gated_n = GroupedRMSNorm(gated): [T,10240] → [T,10240]
8. out = gated + silu(depthwise_conv(gated_n)) + trunk_add:  [T,10240]
8b. conv_state 滑窗更新 (state_len=6)
OUT: out [T, 10240]  (加到 trunk)
```

**搬运账单 (每 token)**: ~320KB (hyper_input 2× 读 40KB + key/key_n/
gated/gated_n 各 [T,10240] 200KB + embeddings 2× 读 10KB + out 20KB +
其他)。仅 1 层 = **2.62GB/forward** (5 环节最小)。

**四维度现状**:
- **寄存器**: GroupedRmsNorm 同 HC (已优化); PleGate 每线程 (t,b) 2560 维
  点积; DepthwiseConvAdd 每线程 (t,c) 读 4 tap + 6 state, 压力低。
- **缓存**: **hyper_input [T,10240] 被读 2 次** (query_n + trunk_add) =
  40KB/token。4 个 [T,10240] 中间张量 200KB/token。
- **SM**: key/value GEMM cuBLASLt SM-saturating; 各 elementwise grid 充足。
- **tensor core**: key/value GEMM cuBLASLt BF16 正确 (PLE 权重在 ignore
  列表, 无 FP8 shadow)。

**手写机会**:

| 融合 | 做法 | 节省 | 风险 | bit-exact |
|---|---|---|---|---|
| **① key/value GEMM 合并** | 拼权重 [2560,12800], 1 GEMM 输出 [T,12800], slice (embeddings 只读 1 次) | **0.4GB** (5KB/token) | 低 | 是 |
| **② hyper_input 2× 读消除** | trunk_add 提前到 GatedValueKernel (或 query_n 计算时顺手加), hyper_input 只读 1 次 | **1.3GB** (20KB/token) | 低 | 大概率是 |

**特殊性**: PLE 核心**不是 GPU 内部带宽, 是 NVMe→pinned→GPU 三级存储
层级** (51.2GB 表随机读 + io_uring 预取 + 独立 stream 重叠, 已优化)。
手写 CUDA/PTX 空间有限 (查表是 NVMe 问题, FP8→BF16 是 elementwise 已优化)。
真正空间是**预取流水线与 48 层计算的深度耦合** (提前更多步预取/ngram ID
预测), 属**调度问题非 kernel 问题** (L3)。

## 4. 总排序与路线图

### 4.1 横向对比 (prefill T=8192)

| 排名 | 环节 | 层数 | 激活/forward | 可省 | 可省比例 | 瓶颈类型 | 手写难度 | 优先级 |
|---|---|---|---|---|---|---|---|---|
| 1 | **GDN 链** | 36 | 72GB | **32GB** | **44%** | 带宽 (elementwise) | 低-中 | **P0** |
| 2 | **HC mix/combine** | 48×2 | 153.6GB | **42.2GB** | **27%** | 带宽 (瘦 GEMM) | 中 | **P0** |
| 3 | MoE | 48 | 94.5GB | 3.8GB | 4% | GEMM (库 <10% 峰值, 手写 tc 是方向) | 中 | P1 |
| 4 | QSA full attn | 12 | 24.2GB | 3.05GB | 13% | 算力 (tensor core 已最优) | 低 | P2 |
| 5 | PLE | 1 | 2.62GB | 1.7GB | 65% | 存储层级 (NVMe) | 低 | P3 |
| **合计** | | | **≈347GB** | **≈82.8GB** | **24%** | | | |

**prefill T=8192 总激活搬运 ≈347GB/forward, 手写可省 ≈82.8GB (24%)** —
这是 Pole 1 (内存屋顶)。Pole 2 (算力屋顶, 手写 tensor core GEMM) 的真实倍数
待 gemm_roofline_bench 实测, 可能更大 (GEMM 占 prefill FLOPs 大头)。两者
合计才是 prefill TTFT 的完整优化空间。

### 4.2 三级路线图 (按用户"分次序、分级别"原则)

**L1 带宽无效搬运 (P0, 先做, 合计 ≈82.8GB/forward)** — 占可省的 100%:

| 序 | 融合 | 环节 | 节省 | 风险 | bit-exact |
|---|---|---|---|---|---|
| 1 | ①+③+④ (prep-norm→conv, conv+state, 4 in_proj 合并) | GDN | 21.6GB | 低 | 是 |
| 2 | HC-B (block_output 去冗余) | HC | 11.5GB | 低 | 是 |
| 3 | ② (NormGate→GDN) | GDN | 10.4GB | 中 | 否 |
| 4 | HC-A (rmsnorm+down+inject 合一) | HC | 30.7GB | 中 | 否 |
| 5 | ①+② (key/value 合并, hyper_input 2× 读) | PLE | 1.7GB | 低 | 是 |
| 6 | ① (d_routed 改 BF16) | MoE | 3.8GB | 低 | 否 |
| 7 | ①+② (5 GEMM 合并, deinterleave+rope) | QSA | 3.05GB | 低 | 是 |

**L2 计算复杂度 (P1, 测量后决定, 需实验)**:
- QSA indexer 降复杂度 (近似打分/降候选块) — 精度-复杂度权衡, 用户要保
  召回, 需实验。
- lm_head 稀疏化 (greedy → top-k 采样) — 改变采样语义, 需实验。

**L3 融合/执行模型 (P2, 局部都优化后)**:
- CUDA Graph + PDL (decode 1.2-1.4×) — 收益最小, 放最后。
- PLE 预取流水线深度耦合 (调度问题, 非 kernel) — 需架构级设计。

### 4.3 建议第一步

**GDN ①+③+④ (低风险、bit-exact、21.6GB/forward)** — 5 环节里**难度最低、
收益确定、风险最小**的切入点, 直接打 prefill TTFT。验证方法论后再做 HC
(高收益、中风险)。

**实施前必须插桩实测** (项目"先测量再决定"原则, 避免"计划 D"式优化错
目标): 在 prefill T=8192 路径上统计每环节激活落 HBM 的字节数, 确认本文
估算的 347GB/82.8GB 是否准确、集中在哪几层。

### 4.4 GEMM roofline 实测 (gemm_roofline_bench, 2026-09-20)

**尺子**: tools/gemm_roofline_bench.cu — 忠实复刻模型 Bf16Gemm 路径
(CUBLAS_COMPUTE_32F + BF16 + FP32 累积 + 禁 split-K), 测 9 个真实 dense
GEMM shape × T∈{1,8,512,8192} 的 cuBLASLt TFLOPS vs SM110a BF16 峰值
(259 TFLOPS)。**这是 Pole 2 (算力屋顶) 的实测基线**。

**T=8192 (prefill 主战场) 关键结果**:

| shape | N×K | TFLOPS | %peak | 解读 |
|---|---|---|---|---|
| GDN in_proj_qkv | 10240×2560 | 158.1 | **61.0%** | 库已接近最优 |
| GDN in_proj_z | 6144×2560 | 136.4 | 52.7% | 库不错 |
| GDN out_proj | 2560×6144 | 132.5 | 51.2% | 库不错 |
| QSA qg | 7168×2560 | 187.2 | **72.3%** | 库已接近最优 |
| QSA o_proj | 2560×6144 | 137.7 | 53.2% | 库不错 |
| **HC down** | 320×10240 | 40.2 | **15.5%** | **瘦 GEMM, 库严重低效** |
| **HC up** | 10240×320 | 74.7 | **28.8%** | **瘦 GEMM, 库低效** |
| HC inject | 10240×2560 | 160.7 | 62.0% | 库不错 |
| lm_head | 248320×2560 | 143.9 | 55.5% | 库不错 |

**T=512 (小 prefill 块)**: 大 GEMM 40-50%, HC down 仅 3.0% (更瘦)。
**T=8 (decode 批)**: 全部 <4% (带宽受限, 已闭合)。

**三个决定性结论**:

1. **库在 BF16 大 GEMM 上 T=8192 达 46-72% 峰值, 不是 <10%**。"<10%" 是
   NVFP4 的事, BF16 库好得多。"手写 tcgen05 赢库 5-6×" 在 BF16 大 GEMM 上
   **不成立** — 库已 61-72%, 手写难超越。**Pole 2 在 BF16 大 GEMM 上没
   那么大**。

2. **真正的库低效点在瘦 GEMM**: HC down (N=320) 15.5%、HC up 28.8%。
   这正是 §3.1 HC 融合靶点 — **但赢的方式是融合 (省中间激活搬运), 不是
   手写 GEMM 提速**。HC down 每 forward 调 96 次 (48 层 × mix+combine),
   15.5% 利用率 × 96 次 = 巨大浪费。

3. **prefill GEMM 时间账 (T=8192)**: GDN 36L×(2.72+1.89+1.94)ms = 234ms
   + QSA 12L×(1.61+1.87)ms = 42ms + HC 48L×2×(1.34+0.72+2.67)ms = 438ms
   + lm_head 72ms ≈ **786ms/forward**。**HC 占 56%** — 印证 HC 是第一靶点,
   但原因是**瘦 GEMM 低效 + 中间激活搬运**, 不是大 GEMM。

**对优先级的修正**: 北极星 §0.0.3 的"Pole 2 可能比 Pole 1 更大"在 BF16 大
GEMM 上**被实测否定** (库已 61-72%)。Pole 2 的真实机会集中在**瘦 GEMM
(HC down/up)**, 且与 Pole 1 (HC 中间激活搬运) **重叠** — HC 融合同时打两
极。这强化了 §4.2 的 P0 = HC + GDN 判断, 但 HC 的赢法是**融合** (HC-A
rmsnorm+down+inject 合一, 省 normed 2× 重读 + 瘦 GEMM 低效), 不是手写
大 GEMM。

### 4.5 E2E 吞吐基线 (evalscope, 2026-09-20)

**尺子**: tools/evalscope/ (uv 环境, `evalscope[perf]` 1.12.0) +
tools/evalscope/run_baseline.sh。serve 配置 `--max-seq 8 --max-prefill 8192
--max-len 16384` (MTP k=3, 连续批处理)。OpenAI 兼容 API, random 数据集,
stream。**这是数据流优化的端到端"尺子" — 任何 kernel 改动最终要在这把尺子
上兑现**。

**短 prompt decode 吞吐 (prompt 256, gen 256)**:

| 并发 | 聚合 Gen/s | TTFT avg | TPOT avg | 单请求 decode |
|---|---|---|---|---|
| 1 | 21.64 | 440ms | 44.4ms | 22.5 tok/s |
| 4 | 31.41 | 1063ms | 74.9ms | 13.4 tok/s |
| 8 | 57.14 | 1965ms | 109.8ms | 9.1 tok/s |

**长 prompt prefill/TTFT (prompt 8192, gen 64, 并发 1)**: TTFT avg
**7.58s**, prefill 吞吐 **~760 tok/s**, TPOT 52ms。

**解读**: ① 单请求 decode 22.5 tok/s 与 STATUS 记录 (~21.8) 一致, 基线可信。
② 并发 8 聚合 57.1 tok/s (2.6× 单请求), 连续批处理有效但 TPOT 从 44→110ms
(单请求变慢换聚合)。③ **长 prompt TTFT 7.58s 是用户 agent 工作负载 (40K-
200K) 的痛点入口** — prefill 760 tok/s 意味着 200K prompt 的 TTFT 将 >4
分钟。prefill 优化 (§4.2 P0 = HC + GDN 融合) 直接打这个 TTFT。④ 40K-200K
超出本 serve 的 max_prefill=8192, 需分块 prefill (PHASES.md 262K 预算),
本轮基线只覆盖 ≤8192。

**优化目标锚点**: 任何 P0 改动 (HC/GDN 融合) 的验收标准 = 这把尺子上长
prompt TTFT 下降 + 短 prompt 聚合 Gen/s 不回归。

### 4.6 prefill T=8192 nsys profile 实测 (2026-09-20) — 推翻 P0 路线图

**尺子**: nsys profile 包裹 `serve --max-seq 1 --max-prefill 8192`, 发 1 个
8194-token 请求 (8.33s wall)。总 GPU kernel 时间 **8.377s**, 时间线窗口
8.317s — **GPU 满负荷, 无 idle 间隙** (host 开销不是瓶颈)。

**GPU kernel 时间构成 (top)**:

| 排名 | Kernel | 时间 | 占比 | 实例 | 文档原判断 |
|---|---|---|---|---|---|
| **1** | **SparseAttentionKernel** | **2.268s** | **27%** | 116 | §3.3 标 P2 "tensor core 已最优" |
| **2** | **GatedDeltaNetRegKernel<8>** | **1.345s** | **16%** | 72 | §3.2 标 "register-state 已落地不动" |
| 3 | GatherQuantKernel (MoE) | 0.418s | 5.0% | 26606 | — |
| 4 | SwiGLUQuantKernel (MoE) | 0.397s | 4.7% | 26606 | — |
| 5 | nvjet 256x256 (GEMM) | 0.359s | 4.3% | 103 | §4.4 估 786ms |
| 6 | CombineWithGateKernel (HC) | 0.347s | 4.1% | 808 | §3.1 P0 |
| 7-8 | MoE FP4 nvjet ×2 | 0.684s | 8.2% | 45783 | — |
| 9 | MixGateKernel (HC) | 0.270s | 3.2% | 836 | §3.1 P0 |
| 10 | CausalConv1dWithCkpt (GDN ①③) | 0.220s | 2.6% | 72 | §3.2 P0 |

**三个决定性发现 (推翻 §3.2/§3.3 优先级)**:

1. **SparseAttentionKernel 是 prefill 最大单点 (2.268s, 27%)**, 不是 GEMM。
   源码确认: grid=(T,nkv)=16384 blocks, 每 block 用 CpAsync16 从 paged KV
   cache **散射读 512 个 topk 位置**的 K/V, 双缓冲 cp.async 隐藏延迟。QK/PV
   mma 仅 ~3.9 TFLOPS (1.5% 峰值) — **纯 KV 散射 gather 带宽/延迟受限, 不是
   算力**。文档 §3.3 "tensor core 已最优" 错了 (它根本不是算力瓶颈)。

2. **GatedDeltaNetRegKernel 1.345s (16%)**, 第二大单点。grid(4,48)=192
   blocks 做 T=8192 的**顺序 recurrent scan** — 20 SM 上 192 blocks 占用率
   低, **延迟受限** (非带宽)。文档 §3.2 "register-state 已落地不动" 错了
   (它占 16%, 是优化靶点)。

3. **GEMM (nvjet) 全部加起来仅 ~1.3s (17%)**, §4.4 的 786ms 估算偏小, 且
   GEMM 根本不是瓶颈。**文档 P0 的 GDN conv+norm 融合只占 4.3%, HC 融合占
   9% — 都不是大头**。

**对路线图的修正**: 文档 §4.2 的 P0 (GDN ①+③+④ + HC) 基于 "347GB 激活字节"
估算, 但**实测时间大头是延迟/带宽受限的 SparseAttention (27%) + GDN 循环
(16%), 合计 43%** — 这两个 kernel 文档恰恰标成 "已最优/不动"。"先测量再
决定" 原则兑现 (计划 D 教训的正面版本): **新 P0 = SparseAttention (2.27s)
+ GDN 循环 (1.35s), 不是 GDN conv/norm 融合 + HC**。

**新优化方向 (待实施)**:
- **SparseAttention (2.27s)**: KV 散射 gather 是根因。方向: (a) 提高 gather
  带宽 (更大 cp.async 批量 / 向量化 / 减少 scatter 粒度); (b) 减少 gather
  字节 (topk 512 是否可降, 需保召回 — 用户约束); (c) 提高 SM 占用率 (16384
  blocks 已多, 瓶颈在每 block 的 scatter 延迟)。
- **GDN 循环 (1.35s)**: 顺序 scan 延迟受限。方向: (a) 提高并行度 (当前
  grid 192 blocks, 20 SM 占用率低); (b) 分块 scan (chunked, 已有
  GatedDeltaNetChunkedKernel 但 prefill 走 Reg 路径); (c) 减少每 token 的
  recurrent 工作量。

### 4.7 SparseAttention topk 排序 E2E 负结果 (2026-09-20, 已回退)

**nsys 偏斜分布 (生产 profile 深挖)**: SparseAttention 116 实例中位数仅
0.54ms, 但 **13 个慢实例 (各 ~170ms, 全部 gridX=8192 即 prefill) 占 97.5%
时间**; 103 个快实例 (gridX=1/2/4, decode) 仅占 2.5%。瓶颈完全在 prefill。

**假设**: topk 位置按 logit 分数序 (任意内存序); prefill 恒等 page table 下
slot==position, 按 position 升序重排 → KV gather 变近似顺序读 → DRAM row
locality 提升。注意力数学对顺序无关 (online softmax 对集合归约), 保正确性。

**实施**: `SortTopkByPosKernel` (每 token 一个 block, bitonic sort 4096
padded 前缀, 48KB smem) 在 SparseAttentionKernel 前重排 d_topk。
q4t_tests 76 项全过 (正确性确认), 零警告。

**E2E 结果 (evalscope, 用户验收标准)**:

| 指标 | 基线 | 改动后 | 判定 |
|---|---|---|---|
| 短 prompt conc 1 | 21.6 tok/s | 21.88 | 持平 |
| 短 prompt conc 4 | 31.4 tok/s | 37.46 | 持平/波动 |
| 短 prompt conc 8 | 57.1 tok/s | ~57 | 持平 |
| **长 prompt 8192 TTFT** | **7580ms** | **7751ms** | **回退 171ms** |

**结论: 回退**。排序 kernel 开销 > locality 收益。根因: bitonic sort 4096
元素 × 144 轮 `__syncthreads()` × 8192 blocks × 12 层, 排序本身比省下的
gather 延迟贵; 且 gather 顺序不是主瓶颈 — kernel 已达 176GB/s (72% 峰值),
剩余 28% 差距是延迟隐藏/cp.async 粒度, 不是地址顺序。

**对 §4.6 方向的修正**: "scatter locality" 方向排除。SparseAttention 剩余
方向: (a) gather 批量/向量化 (cp.async 粒度 16B → 更大); (b) 减少 gather
字节 (topk 512, 需保召回); (c) 延迟隐藏 (双缓冲已做, 看是否够深)。

---

## 5. 约束与风险

1. **decode T=1 全部无意义**: 激活 <0.3%, 纯权重带宽地板 (241 GB/s, 已
   闭合)。所有优化只针对 prefill/批处理。
2. **GEMM 两极框架 (见 §0.0.3)**: 算力受限的 GEMM (prefill 大 M) 方向是
   **手写 tensor core** (tcgen05.mma) 到 >50% 峰值 — 库在 NVFP4 上 <10%
   峰值, 手写赢 5-6×; 我们 BF16 shape 库利用率待测 (gemm_roofline_bench),
   测完再定优先级。带宽受限的瘦 GEMM (HC down/inject) 手写 SIMT 融合省
   搬运。elementwise/recurrent 链 (conv/norm/gate/rope/combine) 是手写
   主战场。
3. **bit-exact vs 噪声验证**: 改运算顺序的融合 (HC-A, GDN-②, MoE-①) 不是
   bit-exact, 需走现有 L2 噪声验证体系 (48 层参考 + near-tie 判据 +
   l2_rel 噪声带)。只改读序/格式的 (HC-B, GDN-①③④, QSA-①②, PLE-①②)
   大概率 bit-exact, 风险低。
4. **所有 GB 数字是估算**: 基于源码静态分析, 未插桩实测。实施前必须实测
   确认, 否则可能"优化错目标"。
5. **MoE ② (persistent) 否决**: 收益大 (17.3GB) 但需重写调度模型, 可能
   失去 nvjet tensor core → 净损失。属架构级改动, 不在 L1 kernel 融合范围。
6. **QSA ③ 不可行**: WriteIndexRaw 依赖 pre-norm 的 ik_raw (D2D copy),
   数据依赖无法合并。

## 6. 与既有工作的关系

- **不推翻现有优化**: GDN register-state (已落地手写哲学)、SparseAttention
  mma.sync 12× KV 削减、nvjet FP4 MoE、FP8 shadow、io_uring PLE 预取 —
  全部保留。本文是在这些之上, 针对**环节间的 HBM 往返**做融合。
- **承接 Phase 3**: [PHASES.md](PHASES.md) Phase 3 列了 "kernel 融合
  (RMSNorm+GEMV, QKV merge, QK_norm+RoPE) / TMA / PDL", 本文是其**细化
  与排序** (给出每融合的具体做法、节省量、风险、bit-exact 性)。
- **验证体系复用**: 所有融合用现有 76 项测试 + tools/verify 48 层参考 +
  bit-exact/L2 噪声判据守正确性。
