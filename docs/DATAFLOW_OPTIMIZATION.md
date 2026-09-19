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
| **L1 带宽** | 带宽挑战 | 消除无效搬运 (读出但没贡献输出的中间激活) | 最大 (与权重读取同量级) |
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

**关键判据**: GEMM 若**算力受限** (prefill 大 M), 保留 cuBLASLt/nvjet
tensor core (已证手写 SIMT 打不过); 若**带宽受限** (瘦维度), 手写融合
可省搬运。elementwise/recurrent 链 (conv/norm/gate/rope/combine) 是手写
主战场。

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

**总激活搬运 (≈347GB) 是权重读取 (~84GB, 含 MoE 专家) 的 4×。** 这是
prefill 的真实瓶颈结构: 不是读权重, 是搬激活。

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

**不碰 up GEMM** (它是 [T,320]×[320,10240] 真 GEMM, prefill 下 tensor core
友好, 手写反而可能更慢 — 保留 cuBLASLt)。

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
| 3 | MoE | 48 | 94.5GB | 3.8GB | 4% | GEMM (nvjet 已最优) | 高 (②否决) | P2 |
| 4 | QSA full attn | 12 | 24.2GB | 3.05GB | 13% | 算力 (tensor core 已最优) | 低 | P2 |
| 5 | PLE | 1 | 2.62GB | 1.7GB | 65% | 存储层级 (NVMe) | 低 | P3 |
| **合计** | | | **≈347GB** | **≈82.8GB** | **24%** | | | |

**prefill T=8192 总激活搬运 ≈347GB/forward, 手写可省 ≈82.8GB (24%)** —
与一次 forward 的权重读取 (~84GB) **同量级**, 是 prefill TTFT 的真实优化
空间。

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

## 5. 约束与风险

1. **decode T=1 全部无意义**: 激活 <0.3%, 纯权重带宽地板 (241 GB/s, 已
   闭合)。所有优化只针对 prefill/批处理。
2. **GEMM 保留 cuBLASLt/nvjet**: in_proj/out_proj/MoE 的 GEMM 若算力受限
   (prefill 大 M), tensor core 已证手写打不过, 不动。手写只针对
   elementwise/recurrent 链 (conv/norm/gate/rope/combine) 和带宽受限的
   瘦 GEMM (HC 的 down/inject)。
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
