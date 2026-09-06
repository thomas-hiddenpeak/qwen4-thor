# STATUS.md — 当前状态快照

> 本文始终反映"现在"。历史状态见 [LOG.md](LOG.md)。

## 当前阶段

Phase 1 — 核心推理引擎 + PLE SSD Stream + HTTP API
(详见 [PHASES.md](PHASES.md))

## 已完成

- [x] 2026-09-03 项目初始化: git 仓库、目录骨架、文档体系、构建配置
- [x] 2026-09-03 模型下载完成 (140 GB, 含 51.2 GB PLE sidecar,
  SHA-256 待验证)
- [x] 2026-09-03 参考项目调研 (qwen35-thor / sglang-ssd-stream /
  thor-probe / thor-bench)
- [x] 2026-09-03 构建骨架验证通过: `q4t version` / `q4t probe` 可运行
  (probe 正确识别 Thor SM 11.0, 20 SM, 122.9 GB)
- [x] 2026-09-03 参考项目克隆到 reference/: sglang-ssd-stream
  (176a522, v0.2.0), qwen35-thor (57e2977)
- [x] 2026-09-03 GitHub 仓库创建并推送 (thomas-hiddenpeak/qwen4-thor)
- [x] 2026-09-03 liburing 2.5 安装 (PLE io_uring 依赖)
- [x] 2026-09-03 PLE ngram 哈希 (row_id) 实现 + 测试:与 SGLang 参考
  逐位一致 (含 EOS-ignoring),multipliers 派生与 checkpoint 一致
- [x] 2026-09-03 PLE io_uring SSD 读取器实现 + 测试:4KiB 页切片/去重/
  注册页池/批量波次读取/scatter;真实 51.2 GB sidecar 上 7 行与 pread
  逐字节一致
- [x] 2026-09-03 PLE FP8→BF16 CUDA 转换 kernel 实现 + 测试: 纯 e4m3→
  BF16 类型转换 (weight_scale 在 PLE 层 forward 的 reduce 之后单独乘,
  与 SGLang 参考一致), 与 CPU e4m3 参考解码在真实 GPU 上逐字节一致
- [x] 2026-09-03 PLE 端到端 gather (PleEmbedding) 实现 + 真实文件验证:
  编排 ngram 哈希 → io_uring reader → FP8→BF16, 真实 checkpoint 参数 +
  真实 51.2 GB sidecar 上 6 token × 16 head × 160 字节与 pread+e4m3 逐字节一致
  **→ PLE 流式层 (核心特性) 全部完成**
- [x] 2026-09-03 IO 层核心: 最小 JSON 解析器 (递归下降, 含 \u 转义) +
  safetensors mmap 读取器 (头解析/张量元数据/按需读字节/H2D), 在真实
  模型 scale 文件上验证; 独立 `q4t_io` 静态库
- [x] 2026-09-04 tokenizer (GPT-2 Byte-Level BPE) 实现 + 差分验证:
  独立 `q4t_text` 库 (ICU 74 NFC 规范化 + `\p{L}` 预分词正则 + 优先队列
  BPE)。encode 做 added-token 整体子串匹配, decode 走 id→content, 均与
  transformers/tokenizers 一致。真实 tokenizer.json 上 57 个多样化输入
  与 python `tokenizers` 库逐位一致 (0 不匹配); 4 个单元测试通过
  **→ IO 层全部完成**
- [x] 2026-09-04 参考项目补全: Qwen3x-Orin (1688f50, tokenizer 参考) /
  thor-bench (3a33a90) / thor-probe (4816685) 克隆到 reference/,
  REFERENCE.md 更新
- [x] 2026-09-04 量化层前置验证: cuBLASLt 原生 NVFP4 (W4A4) 在 Thor
  SM110a 上跑通并数值正确。`CUDA_R_4F_E2M1` +
  `CUBLASLT_MATMUL_MATRIX_SCALE_VEC16_UE4M3`, 真实 expert 投影形状
  (gate/up N=640 K=2560, down N=2560 K=640) × M=1..256 共 16 例全部
  通过 (max_rel < 0.0001, 纯 FP4 量化噪声)。关键: scale 张量必须用
  **128×64 swizzle atom 布局** (非行主序), 物理大小 padding 到完整
  atom `ceil(rows/128)×ceil((K/16)/4)×512`。主数据保持行主序。
  **→ 原生 NVFP4 硬件路径确认可行 (用户要求: 利用硬件特性)**
- [x] 2026-09-04 量化层核心实现 (独立 `q4t_quant` 静态库):
  - `format.h`: e2m1 / UE4M3 编解码 (round-to-nearest-even, **无查表**,
    `__host__ __device__` 双端可用)。UE4M3 与 e4m3fn 正数位布局一致
    (max 0x7E=448, 0x7F=NaN), 与 checkpoint F8_E4M3 一致。
  - `swizzle.h`: NVFP4 scale 张量 swizzle 布局 (128×64 atom) + 物理大小
    padding 计算 + 行主序→swizzle 转换。
  - `dequant.cu`: NVFP4→BF16 dequant kernel (W4A16 路径, 含 global scale)。
  - `act_quant.cu`: BF16→NVFP4 运行时激活量化 kernel (W4A4 路径), 输出
    packed e2m1 + swizzled e4m3, e2m1 按 e4m3-rounded scale 舍入以与硬件
    逐位一致。
  - `fp4_gemm.h`: cuBLASLt 原生 W4A4 GEMM 封装 (CUDA_R_4F_E2M1 +
    VEC16_UE4M3, global scale 折进 alpha)。
  - 测试 `quant_fp4_test.cpp` 9 项: 格式 round-trip / swizzle 偏移+大小+
    round-trip / dequant kernel / 激活量化 kernel / **W4A4 GEMM 8 例**
    (真实 expert 形状 × M=1..256, max_rel < 2e-5)。35 项测试全绿。
  **→ 量化层核心完成 (NVFP4 W4A4 原生路径 + W4A16 dequant 路径)**
- [x] 2026-09-04 量化层收尾 (1/3): NVFP4 routed-expert MoE 权重加载
  编排 (`moe_weights.h/.cpp`, 独立于 `q4t_quant`)。
  - `MoEWeightLayout`: 每层 4 个大 device buffer — 合并 gate/up packed
    `[2*E*moe_is, hs/2]` 行主序 + per-expert swizzled SF 块、down packed
    `[E*hs, moe_is/2]` + per-expert swizzled SF 块、4 个 per-expert FP32
    标量 (weight_scale_2 / input_scale, device + host 副本)。
  - `LoadMoEWeights`: 单次遍历 512 expert, 直载 packed (跳过 ~73K 次
    cudaMalloc), gate/up 的 weight_scale 合并后 host 端 swizzle, down
    单独 swizzle, 标量 H2D + host 副本。
  - **关键约定**: gate_proj 与 up_proj 每 expert 共享相同
    weight_scale_2 / input_scale (checkpoint 已验证) → 合并成单个
    `[2*moe_is, hs]` GEMM 用单一 alpha; 每 expert 的 gate/up 切片步长
    是 `moe_is*hs` 字节 (2 个 proj 宽), 非 `moe_is*hs/2`。
  - 测试 `quant_moe_load_test.cpp` 4 项 (真实 checkpoint): packed 权重
    与 shard 逐字节一致 / SF 反 swizzle 还原到源字节 / gate-up 共享
    scale 全 512 expert 校验 / 加载 expert 的 W4A4 GEMM 与 CPU dequant
    参考一致 (max_rel 0.0)。39 项测试全绿。
  **→ MoE 权重 NVFP4 加载完成 (待补: grouped MoE GEMM 调度 + forward 接线)**
- [x] 2026-09-04 量化层收尾 (2/3 + 3/3): grouped MoE GEMM 调度 +
  input_scale 接线 (`moe_gemm.h/.cu`)。
  - **先钉死 NVFP4 scale 约定** (真实 checkpoint 探针): e4m3 存的是
    "放大后"的块尺度 `= 块尺度 / scale_2` (值 10~20), dequant =
    `e2m1 * e4m3 * scale_2` (乘, 非除)。由此发现 **act_quant kernel
    约定 bug**: 它算 `e4m3 = round(块尺度)` (漏除 input_scale), 会让
    激活重建差 ~1/input_scale (~600×)。修复: 加 `global_scale` 参数,
    `e4m3 = round(块尺度 / global_scale)`, e2m1 按 `e4m3*global_scale`
    舍入。CPU 验证: 错误公式 mean|err| 0.79 → 正确 0.075。
  - `MoERoutedForward(x, expert_ids, router_w, y, weights, ws, ...)`:
    routed-expert 完整 forward (512 expert top-k)。按专家分组:
    BuildTokenLists (atomicAdd 计数) → 每 expert: GatherQuant (token 行
    gather + NVFP4 量化, 用 gu_input_scale) → gate/up GEMM (alpha =
    gu_ws2*gu_in) → SwiGLU kernel → 中间激活量化 (用 **dn_input_scale**)
    → down GEMM (alpha = dn_ws2*dn_in) → ScatterAdd (router 权重加权
    累加到 y)。global scale 在 GEMM alpha 里抵消, 结果 = 真实 dequant
    matmul。
  - 测试 `quant_moe_gemm_test.cpp`: 真实 layer-2 权重, M=4 k=3 路由
    (覆盖 M_e=1/2/3 + 共享 expert), 与完整 CPU 参考 (模拟 FP4 量化 +
    SwiGLU + 加权求和) 一致, max_rel 1.9e-7 (纯 FP32 求和顺序差)。
  - **踩坑**: ① gather 位置 (expert e 的 token 须写到 a_packed 开头
    row 0..M_e-1, 非全局 row e*k+pos); ② router 权重须用 (token,expert)
    在 top-k 的真实 slot, 非 token 在 expert 列表里的 pos (token_list 存
    flat 索引 t*k+slot 解决); ③ 中间激活量化须用 down_proj 自己的
    input_scale (非 gate/up 的)。
  - 40 项测试全绿, 零警告。
  **→ 量化层全部完成** ✅ (格式 / swizzle / dequant / act-quant / GEMM /
  权重加载 / grouped MoE forward, 全部真实 checkpoint 验证)
- [x] 2026-09-04 模型层启动 (1/N): Hyper-Connection (GatedResidual) 主干
  残差 (`hyperconnection.h/.cu`, 独立 `q4t_model` 静态库)。
  - **权威公式核对**: 从 SGLang `python/sglang/srt/layers/hyperconnection.py`
    (@0a79825) 拉取真实 `GatedResidual._mix_compute` / `_combine_compute`。
    `mix`: `normed = GroupedGemmaRMSNorm(hyper_input)` (per-branch,
    `hc_per_branch_norm=true` → 10240 维按 4 组各 2560 独立 RMSNorm, 再乘
    `(1+weight)`); `gate = sigmoid( W_up @ silu( W_down @ normed / hc ) )`;
    `mixed = (gate * normed).view(T,hc,hs).mean(-2)`。`combine`:
    `inject = 2*sigmoid( W_inject @ normed / hc )`;
    `out = R.view(T,hc,hs) + block_output.unsqueeze(1) * inject.unsqueeze(-1)`
    flatten。
  - **关键 bug (已修)**: mix 的低秩门控是 `silu(x / hc)` (先除后 silu),
    不是 `silu(x) / hc` — silu 非线性, 两者差 ~2×。初版写错导致 mix
    max_rel 2.3; 修正后 L2 rel 3.5e-3 (BF16 中间量精度内)。
  - 实现: `GroupedRmsNormKernel` (per-branch 块归约) + `SiluDivKernel` +
    `MixGateKernel` (gate*normed 跨 4 分支均值) + `InjectGateKernel` +
    `CombineKernel`; 低秩 GEMM 复用 `Bf16Gemm` (cuBLASLt)。`LoadHyperConnection`
    从 checkpoint 直载 4 个权重 (mixer 无 block_inject)。
  - 测试 `model_hyperconnection_test.cpp`: 真实 layer-0 attn_hyper_connection
    权重, 随机 [3, 10240] 输入, mix/combine 与 CPU 参考 (模拟 BF16 中间量
    存储) 对比, mix L2 rel 3.5e-3 / combine 2.3e-3。41 项测试全绿, 零警告。
  **→ 模型层 HC 主干完成 (待补: 层内 attn/MLP 接线 + mixer + MTP)**
- [x] 2026-09-05 模型层 (2/N): MoE 完整模块 (`moe.h/.cu`, 并入 `q4t_model`)。
  - 每层 MLP 的完整 forward: router GEMM (`x @ gate^T` [T,512] BF16) →
    top-k kernel (按 logit 选 k=10, **在选中的 k 上 softmax** 归一化, 非
    全部 E — 对照 qwen35-thor `moe_router_topk_kernel` 确认) → routed
    NVFP4 experts (调量化层 `MoERoutedForward`) → shared expert (BF16
    SwiGLU, gate/up 合并单 GEMM) → 门控组合
    `out = routed + sigmoid(x @ shared_expert_gate) * shared_down`
    (不加 residual, residual 由外层 HC combine 处理)。
  - `LoadMoEExtra` 从 checkpoint 直载 5 个 BF16 权重 (gate [512,2560] /
    shared_expert.{gate,up}_proj [640,2560] 合并 / shared_expert.down_proj
    [2560,640] / shared_expert_gate [1,2560])。
  - **踩坑 (已修)**: ① `MoEForwardWorkspaceBytes` 运算符优先级 bug —
    `(routed+7) & ~7 + align8(scratch)` 因 `+` 高于 `&` 算成 ~38KB (正确
    ~547KB), scratch 全越界; compute-sanitizer 定位到 cuBLASLt
    splitKreduce_kernel 越界写。② `Bf16Gemm` 对 tall-skinny 形状 (M=2,
    K=2560) 选 split-K 算法, 其 splitKreduce 在 SM110a 越界写 — 在
    heuristic 偏好里 `CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK=NONE`
    禁用 split-K。③ 测试 CPU 参考把单个 gate_proj 张量读进 2× 大小 buffer,
    up 半未初始化垃圾 (ReadTensor 只写张量实际大小) — 改分别读 gate/up 再
    拼接。
  - 测试 `model_moe_test.cpp`: 真实 layer-2 routed NVFP4 + BF16
    router/shared 权重, T=2 k=10, 与完整 CPU 参考 (top-k + NVFP4 dequant
    routed + BF16 shared + 门控组合) 一致, L2 rel 1.7e-3。42 项测试全绿,
    零警告。
  **→ 模型层 MoE 完整模块完成 (待补: attn + 层组装 + mixer + MTP)**
- [x] 2026-09-05 模型层 (3a/N): linear_attention (Gated DeltaNet SSM)
  (`linear_attention.h/.cu`, 并入 `q4t_model`)。
  - qwen4_exp 的 linear_attention 层 (36 层) 继承 Qwen3.5 GatedDeltaNet。
    完整 forward: 投影 (in_proj_qkv [T,10240] q|k|v + in_proj_z [T,6144] +
    in_proj_a/b [T,48]) → causal conv1d (kernel 4, SiLU, 持久 conv_state
    [10240,3]) → Gated DeltaNet 递归 (SSM state [nv=48, kd=128, vd=128],
    每 value head 一个 block, S 放 shared memory) → 融合 per-head RMSNorm *
    silu(z) gate → out_proj [T,2560]。qwen4 linear 层**无 attn_output_gate**
    (与 full attention 不同)。
  - `LoadLinearAttention` 直载 9 个权重 (in_proj_qkv/z/a/b, conv1d, out_proj,
    norm, A_log, dt_bias)。
  - **踩坑 (已修)**: ① q/k 归一化是 L2 风格 `k/sqrt(sum(k^2)+eps)` (**不除
    kd**), q 额外乘 1/sqrt(kd) — 初版误用 RMSNorm (多除 kd)。② softplus/
    alpha 指数: 参考 `exp2f(x*LOG2E)` (=e^x), 初版误写 `expf(x*LOG2E)`
    (=e^(1.4427x)); 因 t=0 时 delta=v 不依赖 alpha, 误差随 token 线性增长,
    按 token 分解定位。③ 中间量须单独 cudaMalloc, 不能从 workspace carve
    (workspace 同时是 cuBLASLt scratch)。
  - 测试 `model_linear_attention_test.cpp`: 真实 layer-2 权重, T=4, 零初始
    state, 与完整 CPU 参考一致, out L2 rel 7.5e-3 / ssm_state 5.0e-3。43 项
    测试全绿, 零警告。
  **→ 模型层 linear_attention 完成 (待补: full_attention/QSA + 层组装 +
    mixer + MTP)**
- [x] 2026-09-05 模型层 (3b/N): full_attention (QSA 稀疏注意力)
  (`full_attention.h/.cu`, 并入 `q4t_model`)。
  - qwen4_exp 的 full_attention 层 (12 层, 每第 4 层) = GQA (24 q / 2 kv
    head, head_dim 256) + partial MRoPE (rotary_dim 64 = 0.25*256, theta 1e7)
    + attn_output_gate + QSA indexer。完整 forward: 投影 (qg [T,12288]
    Q+Gate 每 head 交错 / k [T,512] / v [T,512]) → deinterleave qg→q,gate +
    per-head **centered** RMSNorm(q) → centered RMSNorm(k) → partial RoPE
    (前 64 维) → 写 KV cache → QSA indexer (iq/ik 投影 + GemmaRMSNorm plain
    + RoPE + 4-token 平均池化压缩 K + MQA relu logits + block top-512 →
    2048 token 索引) → 稀疏 GQA 注意力 (online softmax) → *sigmoid(gate) →
    o_proj [T,2560]。
  - **关键洞察**: 序列 ≤2048 token 时可见压缩 block 数 ≤512 = block_topk,
    QSA 退化为稠密因果注意力 (topk[t]=[0..t]); 稀疏仅在 >2048 生效。
    indexer 仍完整运行以匹配参考。
  - **踩坑 (已修)**: ① **BF16 位转换** — `__nv_bfloat16(uint16_t)` 无"原始
    位"构造, 会把 u16 整数提升为 float 再转 BF16 破坏位模式 (纯拷贝都错)。
    改用 `memcpy` 位操作 (同 linear_attention)。② `BuildCompressedKKernel`
    group 索引 `(t+1)/compress` 应为 `t/compress` (t=7 时越界读 positions[8],
    污染 CUDA context 致后续 kernel 全错, compute-sanitizer 定位)。③
    稀疏注意力点积须全维 `sum_j q[j]*K[c][j]` (初版误用单标量 qv)。④
    topk 选择用单线程串行避免共享内存竞态。
  - 测试 `model_full_attention_test.cpp`: 真实 layer-3 权重, T=8 (QSA 稠密
    退化区), 与完整 CPU 参考 (投影 + centered RMSNorm + partial RoPE + 稠密
    因果 GQA + sigmoid gate + o_proj) 一致, out L2 rel 4.6e-3。44 项测试
    全绿, 零警告。
  **→ 模型层 full_attention/QSA 完成 (待补: 层组装 + mixer + MTP)**
- [x] 2026-09-05 模型层 (4/N): decoder layer 组装 (`decoder_layer.h/.cu`,
  并入 `q4t_model`)。
  - 把已验证的子模块 (HC mix/combine + linear/full attn + MoE) 接线成完整
    decoder layer forward: `attn_hc.mix` → attn block → `attn_hc.combine` →
    `mlp_hc.mix` → MoE → `mlp_hc.combine`。`DecoderLayer` 持有全部子模块
    权重 + per-layer 持久 cache (linear SSM/conv, full KV/indexer),
    `LoadDecoderLayer` 按 layer_id 自动选 linear/full 并加载 4 组权重
    (attn_hc / mlp_hc / attn block / MoE)。
  - **关键**: 单一 device workspace 按子模块 carve, 每个 offset 必须
    **256 字节对齐** (cuBLASLt 拒绝未对齐 scratch 指针, 否则 INVALID_VALUE)。
  - PLE 注入 (layer 2, attn_hc.mix 之前) 尚未接线 (PLE 层 short-conv +
    key/value proj + gated reduce 是独立模块), 当前 `has_ple=false`。
  - 测试 `model_decoder_layer_test.cpp`: 真实 layer-0 (linear, 无 PLE),
    用两条独立路径 (生产 `DecoderLayerForward` vs 手动分步调用子模块) 从
    相同零初始状态出发, 输出逐位一致 (A-vs-B L2 rel 0.0)。45 项测试全绿,
    零警告。
  **→ 模型层 decoder layer 组装完成 (待补: PLE 注入 + mixer + MTP + 层循环)**
- [x] 2026-09-05 **Paged KV cache (PD-ready 前提)**: full_attention KV
  从连续 `[max_len, nkv, 2, hd]` 改为**按页组织 + 页表间接寻址**
  (`kKvPageSize=16`, `page_table[p] = p/16` 恒等映射下与旧布局逐位
  一致)。`WriteKVKernel`/`SparseAttentionKernel` 加页表参数, `DecoderLayer`
  持有 `page_table`, `ResetState`/`Free`/`LoadDecoderLayer` 同步更新。
  52 项测试全绿 (decoder_layer A-vs-B l2_rel 0.0) + 长序列生成验证
  (prompt 1612 + decode 523, 越过 2048 稀疏激活点, 输出全程连贯)。
  期间发现并修复 2 个**预先存在**的越界 bug (见"已解决")。
- [x] 2026-09-05 **PD-ready 阶段边界 API (ModelSequence)**: 引擎把
  "完成 prefill、交出 KV/SSM 状态"暴露为独立操作, 供 runner 驱动
  prefill 与 decode 为两次调用。`ModelSequence` 是轻量 host 状态机
  (stage: kIdle→kPrefill→kDecode, position, PLE history)。4 个操作:
  `ModelBeginSequence` (重置 per-layer 状态, stage=kPrefill) →
  `ModelPrefill` (完成 prefill, stage=kDecode, **KV/SSM 状态就绪的
  交接点**) → `ModelDecodeStepSeq` (单 decode token, 自动维护
  position/history) → `ModelEndSequence` (重置 kIdle)。`ModelForward`
  重构为 `ResetAllLayers + RunPrefill` (向后兼容)。`main.cpp` /
  `chat_server.cpp` 改用序列 API。测试 `model_sequence_api`: prefill
  legacy-vs-seq 逐位一致 + decode legacy-vs-seq 逐位一致 + 状态机转换
  校验。53 项测试全绿, 零警告。生成验证 (27+48) 连贯。
  **→ PD-ready 架构 Phase 1 部分全部完成** (Paged KV + 可分离代码路径 +
  阶段边界 API; 完整多设备 PD 部署归 Phase 2)

## 进行中

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
    原始 router 分数在 ~1e-3 内的真近并列, 良性 MoE 行为。E8 证明状态处理无 bug;
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

## 阻塞 / 风险

- **PD-ready 架构 (Phase 1 可分离性已全部完成)**: runner 后期特殊
  场景需 PD 分离, 架构须早期可分离。Phase 1 落地可分离性:
  ✅ Paged KV cache; ✅ prefill/decode 可分离代码路径; ✅ 阶段边界 API
  (ModelSequence, 引擎暴露"完成 prefill、交出 KV/SSM 状态"为独立操作)。
  完整多设备 PD 部署 (连续批处理 + 多请求调度) 归 Phase 2。
  设计见 ARCHITECTURE.md, 范围见 PHASES.md 第 6 项。
- **PLE sidecar SHA-256 未验证** (ssd-stream.json 记录了期望值
  `b070f964...`, 51.2 GB 校验耗时较长, 安排在首次加载前完成)。
- **MTP 待用户排期** (权威参考已就位: `reference/vllm/vllm/models/
  qwen4_exp/nvidia/mtp.py`, 见"进行中" ⏳ 条目), 等整体架构完善后推进。
- **MoE 贪心非确定性** (见"进行中"长序列条目): `ScatterAddKernel` 的 FP32
  `atomicAdd` 顺序非确定, 运行间 argmax 可能翻转。属 LLM 固有特性 (PyTorch
  同样), 不影响正确性; 如需可复现输出, 可改确定性归约 (代价: 性能)。

## 已解决 (2026-09-05)

- ✅ **MoE `BuildTokenListsKernel` 越界 (预先存在, Paged KV 验证时暴露)**:
  `token_list` 分配为 `[E, k]` (512×10=5120 int), 但 `token_list[e*k+pos]`
  的 `pos` 是 expert e 累计收到的 token 数 — 一个 expert 最多可被 **M** 个
  token 选中 (M>k 时越界)。单元测试 M=2 (<k=10) 从不触发; 之前生成运行中
  OOB 写落已映射统一内存 (静默损坏), Paged KV 加 `page_table` 分配后布局
  偏移使 OOB 落入未映射区 → hard fault。compute-sanitizer 定位 (3088
  errors, 全在此 kernel)。修复: `token_list [E,k]→[E,M]`, 三处索引
  `e*k+row→e*stride+row` (stride=M)。
- ✅ **`d_logits` 越界 + 读错行 (main.cpp + chat_server.cpp, 预先存在)**:
  `d_logits` 只分配 `vocab*2` (1 行), 但 prefill lm_head GEMM 输出
  `[T, vocab]` (T 行) → 越界写 T-1 行 (nvjet kernel illegal address,
  compute-sanitizer 定位)。且 prefill 后读 `d_logits[0..vocab)` 是**第 0 行**
  (prompt 首 token logits), 非最后一行 → 首 token 错误。修复: 分配
  `T*vocab*2`, prefill 后读第 T-1 行 (decode T=1 写第 0 行, 兼容)。
  验证: 短 prompt (27+64) 连贯 + 长序列 (1612+523, 越过 2048) 全程连贯。

## 已解决 (2026-09-04)

- ✅ **constexpr 查表数组在 device 端无存储**: `format.h` 初版用
  `inline constexpr float kE2m1Table[16]` / `kE4m3Pow2[15]` 做运行时索引
  解码。CMake 构建 (带 `--expt-relaxed-constexpr`) **编译通过**, 但
  namespace 作用域的 constexpr 数组在 device 代码里**没有存储**, 运行时
  索引读到垃圾 → dequant/act_quant kernel 输出全 0 或错值, 而 W4A4 GEMM
  测试仍 PASS (cuBLASLt 用硬件自己的 e4m3 解码, 不调用我的函数), 掩盖了
  bug。修复: 解码改为**无查表** (e2m1 按位分解 + `ldexpf`, e4m3 按位 +
  `ldexpf`), 全部 `__host__ __device__`。教训: ① 不要在 device kernel 里
  用运行时索引的 constexpr 数组; ② 单元测试必须直接验证 kernel 输出,
  不能只靠端到端 GEMM (会掩盖底层格式错误)。
- ✅ **cuBLASLt 原生 NVFP4 scale 布局破解**: `VEC16_UE4M3` 的 scale
  张量**不是行主序**, 而是硬件 tcgen05.mma 要求的 **128 行 × 64 元素
  swizzle atom** (CUTLASS `SfKMajorAtom`)。逻辑坐标 (r 行, g 组, g=K/16)
  → offset: `i=r%32, j=(r%128)/32, ga=g%4, within=i*16+j*4+ga,
  offset=within+(g/4)*512+(r/128)*((K/16)/4)*512`。物理大小必须 padding
  到完整 atom: `ceil(rows/128)*ceil((K/16)/4)*512` (即使 M=8 也按 128
  行分配, 否则堆越界)。主数据 (FP4 packed) 保持行主序 [N, K/2]。
  用 CuTe `tile_to_shape(SfAtom, (M,K), Step<_2,_1>)` 探针验证公式,
  16/16 真实形状用例通过 (max_rel < 0.0001)。

- ✅ **NVFP4 scale 约定钉死 + act_quant 约定 bug**: 用真实 gate_proj 权重
  探针确认: e4m3 存的是"放大后"的块尺度 `= 块尺度 / scale_2` (值 10~20),
  dequant = `e2m1 * e4m3 * scale_2` (乘)。`e2m1*e4m3*scale_2` → std 0.0127
  合理, `/scale_2` → std 189430 荒谬。据此发现 act_quant kernel 初版算
  `e4m3 = round(块尺度)` (漏除 input_scale), 激活重建差 ~1/input_scale
  (~600×)。修复: 加 `global_scale` 参数, `e4m3 = round(块尺度/global_scale)`,
  e2m1 按 `e4m3*global_scale` 舍入。CPU 验证 mean|err| 0.79→0.075。教训:
  "自洽对比" (kernel 与 CPU 参考用同一约定) 抓不到约定级错误, 必须用
  **独立数据源** (真实 checkpoint 的 e2m1/e4m3/scale_2 数值量级) 钉死约定。
- ✅ **grouped MoE forward 三个坑**: ① gather 位置 — expert e 的 token 须
  写到 a_packed 开头 row 0..M_e-1 (GEMM 读前 M_e 行), 非全局 row e*k+pos;
  ② router 权重 slot — 须用 (token,expert) 在 top-k 的真实 slot, 非 token
  在 expert 列表里的 pos (token_list 存 flat 索引 t*k+slot 解决); ③ 中间
  激活 (SwiGLU 输出) 量化须用 down_proj 自己的 input_scale, 非 gate/up 的。
  另: GEMM 里 global scale 经 alpha 抵消, 结果 = 真实 dequant matmul, CPU
  参考须按真实 dequant (权重×weight_scale_2, 激活×input_scale) 且不再乘
  alpha。最终 max_rel 1.9e-7。
- ✅ **MoE 合并 gate/up 切片步长 bug**: `gu_packed_expert(e)` 初版用
  `e * moe_is * hs/2` (单 proj 步长), 但每 expert 的合并切片含 gate+up
  共 `2*moe_is` 行, 步长应为 `e * moe_is * hs`。错误导致 expert e≥1 的
  gate 覆盖 expert e-1 的 up。W4A4 GEMM 测试**没抓到** — 它只用 expert 0
  (偏移 0, 不受步长影响) 且是 device buffer 自洽对比 (CPU 参考也 dequant
  同一 buffer)。修复后靠 `moe_load_packed_matches_shard` 对 expert
  {0,100,511} 的 gate/up/down 与 shard 逐字节比对才暴露。教训: ① 步长/
  偏移类 bug 必须用**多个非零索引** + **与独立数据源 (shard) 比对** 才能
  抓到, 单点 + 自洽对比会掩盖。

## 已解决 (2026-09-03)

- ✅ **PLE 查找机制完全理解**: PLE = n-gram 哈希查找表。
  每 token 取 [t-2,t-1,t] 3-gram 上下文, 16 个 head 各算一个
  哈希 row_id (splitmix 派生乘子 + 素数词表取模), 查 16 行
  (160B FP8) 拼成 2560 维嵌入, 经 key/value 投影 + 门控 +
  depthwise conv 后加到主干。详见 MODEL.md。
- ✅ **hc_*/indexer_*/ngram_* 字段语义确认**:
  hc_count=4 (hyper-connection 4 分支主干), indexer_* = QSA
  稀疏注意力索引器配置, ngram_* = PLE 查找参数。
- ✅ **PLE io_uring 读取器实现并验证**: 镜像 sglang-ssd-stream 的
  gather 语义 (行→4KiB 页对齐切片 Piece → 按 page_id 排序去重分组
  PageGroup → 4096 页/批、256 页/波 io_uring 读 → scatter 到输出;
  越界行输出置零)。32MiB 注册页池 (mmap + io_uring_register_buffers,
  失败回退普通读)。真实 51.2 GB sidecar 上 7 行 (0/1 同页、25/26 同页、
  1000000、320001000、末行 320001535) 与 pread 逐字节一致。
  坑: `io_uring_submit_and_wait` 成功时返回**提交的 SQE 数**(≥0),
  非 0; 错误判断须用 `< 0`。

## 下一步

Phase 1 已完成的层: **PLE 流式层** ✅, **IO 层** ✅, **量化层** ✅
(NVFP4 W4A4 原生 + grouped MoE), **模型层** ✅ (48 层 forward + PLE
注入 + head/tail + generate + 长序列 QSA 稀疏路径), **serve** ✅
(OpenAI 兼容 HTTP API), **PD-ready 架构** ✅ (Paged KV + 可分离代码
路径 + 阶段边界 API ModelSequence), **decode 路径自洽性** ✅
(prefill/decode 同 token 对照, SSM state FP32, conv1d 窗口 + PLE
short-conv 持久状态均已修复, batch vs incremental 逐位一致)。
54 项测试全绿, 零警告。

剩余 Phase 1 项 (按 2026-09-06 与用户确认的顺序):
1. **逐 token 对参考验证 (进行中)**: 4 层基线已完成 (4/4 argmax 匹配,
   见上)。decode 路径自洽性验证已完成 (见下: conv1d 窗口 + PLE
   short-conv 持久状态两个 bug 均已修复, C++ batch vs incremental
   逐位一致, 参考对照 4/4 argmax 匹配)。下一步: 扩展到更多层 /
   更长 token 序列, 钉死正确性基线 (性能优化的守护网)。
2. **prefill/decode 性能优化**: 用 ① 的基线守护数值不回归; 同时**预留
   MTP 接口** (scheme A: 主模型收尾阶段可选暴露 pre-final-mixer 多流
   [T, hc*H] 给 MTP 第一步), 避免优化后再返工。
3. **MTP 1 层**: 在 ② 的稳定基线上开发 + 测加速比 (用户决定: 性能优化
   后在良好基线上进行)。权威参考: `reference/vllm/vllm/models/
   qwen4_exp/nvidia/mtp.py` (transformers 5.16.1 **无** MTP, 加载时
   显式跳过 mtp.* 权重)。
4. **多模态图像输入**: transformers 5.16.1 提供权威参考 (Qwen4ExpVision
   Model 27 层 ViT + masked_scatter 融合 + M-RoPE), 之前"最大缺口且无
   权威参考"已解除。可与 ②③ 穿插, 不阻塞 MTP。
5. **PLE 工作内存 <100 MiB 验证** + **PLE sidecar SHA-256 校验**。

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
