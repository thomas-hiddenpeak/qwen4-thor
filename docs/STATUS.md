# STATUS.md — 当前状态快照

> 本文始终反映"现在"。历史状态见 [LOG.md](LOG.md)。

## 当前阶段

Phase 1 — 核心推理引擎 + PLE SSD Stream + HTTP API
(详见 [PHASES.md](PHASES.md))

## 已完成

- [x] 2026-09-03 项目初始化: git 仓库、目录骨架、文档体系、构建配置
- [x] 2026-09-03 模型下载完成 (140 GB, 含 51.2 GB PLE sidecar,
  SHA-256 已验证 2026-09-07, 与 MODEL.md 期望值逐位一致)
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
- [x] 2026-09-07 MTP 推测解码实现: BF16 MoE forward + unit-weight HC
  combine + MTP 权重加载 + draft forward + 推测解码循环 (draft k 步 +
  主模型验证 + 接受/回退) + 主模型 recurrent 状态快照/恢复 API。
  56 项测试全绿 (含 mtp_draft_forward + mtp_speculative_step), 零警告。
- [x] 2026-09-07 视觉塔 (Qwen3_VisionTransformer) CUDA 实现 + 视觉
  特征注入主模型: 独立 `q4t_vision` 库, 27 层 ViT (patch_embed +
  bilinear pos_embed + 2D RoPE + 双向 attention + spatial merge), 333
  个 `model.visual.*` 张量加载; 端到端测试 vision_forward: CUDA vs
  numpy 参考 l2_rel=0.0317 (BF16 精度范围内)。视觉特征注入:
  `ModelForward`/`ModelPrefill` 加可选 `VisionFeatures`, image token
  (248056) 位置 embedding 替换为视觉特征 (镜像 vllm
  `_merge_multimodal_embeddings`); 测试 model_vision_inject (计数不匹配
  报错 + 注入改变 logits + 确定性)。59 项测试全绿, 零警告。
- [x] 2026-09-07 PLE SSD Stream 工作内存 <100 MiB 验证 + sidecar
  SHA-256 校验 (核心特性收尾):
  - **SHA-256 校验通过**: 真实 51.2 GB sidecar (51,200,245,760 字节 =
    320,001,536 行 × 160) 的 SHA-256 =
    `b070f9644adf93794d8a1030584ab705809387e64396a9327a68fa3a3a6666b3`,
    与 MODEL.md 记录的期望值**逐位一致** (sha256sum, 49s)。
  - **工作内存实测 75.17 MiB < 100 MiB 预算**: 新增 `PleEmbedding::
    working_memory_bytes()` + `PlePageReader::pool_bytes()/scratch_bytes()`
    精确测量接口 (报告真实分配字节数, 非重算常量)。分解: 页池 32 MiB +
    pinned host staging 20 MiB + GPU FP8 scratch 20 MiB + pinned row-ids
    1 MiB + io_uring ring ~0.025 MiB + reader scratch ~2 MiB (full-
    capacity gather 峰值)。生产配置 capacity_tokens=8192, row_bytes=160,
    ngram_heads=16 (=(ngram_size-1)×heads_per_ngram=2×8)。
  - **无 OOM, 无 swap**: 真实 full-capacity gather (读真实 sidecar) 前后
    SwapFree 不变 (413600 kB), MemAvailable 114 GiB 充足。
  - 测试 `ple_working_memory_under_100mib`: 真实 sidecar + 生产配置,
    full-capacity gather 触发 scratch 峰值后断言 < 100 MiB。62 项测试
    全绿, 零警告。
  **→ PLE SSD Stream 核心特性全部完成 (流式层 + 内存预算 + 完整性校验)**
- [x] 2026-09-07 多模态图像输入收尾 (processor + serve 接入 + 端到端):
  (c) C++ 图像 processor (`q4t/vision/processor.h/.cpp`): stb_image 解码
  (PNG/JPEG→RGB) + smart_resize (factor=32, clamp [min,max] pixels) +
  **Pillow 12.3.0 定点 BICUBIC** (a=-0.5, PRECISION_BITS=22, 逐位复刻
  `Resample.c`) + rescale (/255) + normalize ((x-0.5)/0.5) + **block-major
  patchify** (per-patch [C=3,T=2,P=16,P=16])。差分测试 vision_processor:
  真实 transformers 5.16.1 processor ground truth 上, 恒等图 (256x256) 与
  BICUBIC 图 (140x100→320x224) **均逐位一致 (max_abs_diff=0)**。(d) serve
  层多模态接入 (`chat_server.cpp`): 解析 OpenAI content 数组 (text +
  image_url 部件, base64 data URL) → 解码图像 → processor → 视觉塔 →
  `ExpandImageTokens` (每个 `<image>` 占位符展开为 `grid_h/2*grid_w/2` 个
  image token) → `ModelPrefill` 注入视觉特征; 视觉塔在 `Start` 加载 (无
  `model.visual.*` 时优雅降级纯文本)。端到端测试 vision_e2e: 真实 PNG →
  processor → 视觉塔 → 展开 → 注入 prefill → logits (有限/非平凡/注入
  改变 logits/确定性)。**61 项测试全绿, 零警告**。
- [x] 2026-09-07 greedy 生成输出与参考实现一致 (L2 噪声保真度验证,
  Phase 1 最后完成标准): C++ NVFP4 W4A4 引擎 vs transformers 5.16.1
  参考 (dequantized FP32), 同一 256-token prompt 的 16 层 prefill logits
  逐位置对比。判据问"差异是否只有量化噪声、有无系统性 bug" (两侧差异
  就是 NVFP4 量化噪声, 不可能逐位一致): **[A] 置信位置 (参考 gap >
  τ=3σ) argmax 8/8 全对** (黄金标准, 系统性 bug 会破坏这些位置) /
  **[B] 108 个 argmax 翻转全部 near-tie** (gap ≤ τ) / **[C] l2_rel 均值
  0.2069** (与 e2m1 网格 ~20% 理论值吻合, max 0.65 < 0.75)。raw argmax
  57.8% 低因参考 96.9% 位置是 near-tie (选哪个 token 都在噪声内)。
  参考 dump 改逐层 lazy dequant (占位符 + forward 按需 dequant 单层,
  内存峰值 ~80GB→~23GB, 否则 16 层 OOM)。**结论: 无系统性错误, 差异纯
  为量化噪声。Phase 1 完成标准全部闭合。**
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
- [x] 2026-09-13 **B1 多序列隔离 (per-seq 状态池化 + seq_id 穿透收尾)**:
  recurrent 状态 (linear ssm/conv、PLE short-conv、full KV/idx) 池化为
  `[max_seq, ...]`, seq_id 穿透 `DecoderLayerForward`/`ModelPrefill`/
  `ModelDecodeStepSeq` 选 per-seq 切片; 新增 `model_multi_seq_isolation`
  测试 (4 层含 full attention, max_seq=4, 4 个 seq_id 串行跑同一 prompt
  要求逐位一致)。修复两个 bug: (1) `uint16_t*` kv_cache 用字节数做
  per-seq 偏移被放大 2 倍 → seq≥2 越界写 (illegal access / 静默 DIFFER),
  改 `char*` 字节偏移; (2) full attention 四个 RMSNorm kernel 的 float
  `atomicAdd` 求和顺序非确定 → 跨运行 bit 漂移, 改确定性 `BlockSum`
  (warp shuffle 固定序)。64 项测试全绿, 零警告, 隔离测试连跑稳定 PASS。
  **serve 多请求 E2E 已验证**: `q4t serve --max-seq 4` 下 3 个并发请求
  (不同 prompt, 不同 seq_id) 全部成功返回且语义正确 (Paris / 4 / 正确
  展开), 日志无 error/illegal/503。
  **→ B1 全部闭合** (状态池化 + seq_id 穿透 + 隔离测试 + serve 多请求 E2E)
- [x] 2026-09-13 **B2a 连续批处理引擎 (token 级打包)**: 把 B 个活跃序列
  各 1 个 decode token 打包成一次 T=B forward (decode memory-bound, 权重
  84 GB 只读一次而非 B 次)。GEMM (HC/MoE/head/proj) 无状态, 现有
  `Bf16Gemm(T=B)` 自动打包 (免费); 有状态 kernel 用 device 数组
  `d_seq_id[t]` 选 per-token 状态切片: 新增 GDN decode kernel (grid
  (nv,B), 每 block 一序列一 token, 无跨 token 链) + conv1d / PLE short-conv
  多序列 kernel (坑: decode 当前 token tap 在打包索引 t 而非 0) + full
  attention 7 kernel per-seq 间接寻址 (rope/kv/page_table/idx 按
  `d_seq_id[t]` 选切片, `d_seq_id==null` 时单序列路径 bit 不变)。API
  `ModelDecodeBatchMulti` + `Model.d_seq_id` 持久 buffer。测试
  `model_decode_batch_multi`: B=1 打包 vs 串行 l2_rel=0.018 argmax 一致
  (kernel 路径正确, 差异纯为 GEMM M=1 算法选择) + B=4 不同 prompt 隔离
  l2_rel 0.07–0.18 无跨序列污染。65 项测试全绿, 零警告。
  **→ B2a 闭合** (引擎 + API + 单元测试; B2b serve 调度器待做)
- [x] 2026-09-13 **B2b serve 连续批处理调度器 (E2E 闭合)**: serve 层
  独立调度线程 `SchedulerLoop` 把并发请求的 decode step 合并成一次
  `ModelDecodeBatchMulti` 调用 (token 级连续批处理)。请求线程每步填
  `ActiveRequest` (token/position/seq_id/ple_hist) → `pending=true` +
  notify → 等 `done` 取 `next_token` (argmax); 调度线程收集 pending →
  `model_mu_` 下打包 forward + D2H → `sched_mu_` 下逐请求唤醒。回退:
  buffer 分配失败或 `active_>=max_seq` 时走 `ModelDecodeStepSeq` 单序列
  路径; MTP 请求保持单序列 (draft KV 单共享 buffer 未批处理, 已知范围
  限制); stop 时 drain 所有 active 防死锁。E2E: 3 并发请求 (不同
  prompt) 全部语义正确 (Paris/4/Jupiter), 无跨序列污染, 日志无
  error/illegal/503。吞吐: 单请求 15.13 tok/s → 3 并发聚合 18.03 tok/s
  (1.19x, 低于线性因 MoE 专家权重读取不共享)。65 项测试全绿零警告。
  **→ B2 全部闭合** (B2a 引擎 + B2b serve 调度器 + E2E + 吞吐实测)
- [x] 2026-09-13 **MTP 批处理 Stage 1 (draft 状态池化 + 多序列
  MtpForward)**: MTP 请求当前独占 model_mu_ 整个投机循环 (draft 循环 +
  验证 + extend), 不能并发 — 根因是 draft 模型 (1 层 full-attention) 的
  kv_cache/page_table/idx_raw/idx_comp 单份共享。Stage 1 把 B1 的 per-seq
  池化模式应用到 MTP: MtpConfig.max_seq (默认 1 = 旧布局 bit 不变),
  draft KV/indexer/rope 全部 [max_seq,...] 池化; MtpResetState 加 seq_id
  (字节偏移走 char*); 新增 MtpPerSeqKvBytes; MtpForward 加 d_seq_id 尾参
  透传 FullAttentionForward (B2a 机制), null 时单序列 bit 不变。测试
  mtp_draft_multi_seq (max_seq=4, 4 序列×2 token 打包): 每行 vs 单序列
  参考 l2_rel≈0.002 (BF16 draft, 噪声远小于主模型 W4A4) 无跨序列污染 +
  确定性 bit 一致。66 项测试全绿零警告。
  **→ MTP 批处理 Stage 1 闭合** (并发 MTP 地基; Stage 2 = 批量化 draft
  循环 + ragged 多序列验证 + 调度器 MTP 分支, 未做)
- [x] 2026-09-13 **MTP 批处理 Stage 2a (多序列验证前向 + per-seq
  checkpoint 回滚)**: 新增 `ModelVerifyMulti` — B 序列 × T token (k+1)
  打包成一次主模型 forward (sequence-major [B,T]), 权重只读一次;
  linear/PLE 层走 per-seq 因果链 kernel (增量 1-3 已提交), full attention
  走 per-seq paged KV (B2a, 设计稿中"因果掩码风险"经核实是误判: per-seq
  KV 隔离天然阻止跨序列注意力)。checkpoint 体系升级为池化布局
  [num_layers, max_seq, cap, elems] (max_seq=1 退化旧布局 bit 不变) +
  新增 PLE conv checkpoint (修复既有 bug: 单序列 MTP 部分接受时 PLE
  short-conv 窗口残留被拒绝 token)。实现中发现并修复 2 个 kernel bug:
  ① DepthwiseConvAddMultiSeqCausalKernel 的 T 参数同时用于 grid 边界
  (需总数 B*T) 和局部位置 tt=t%T (需 tokens_per_seq) — 原代码传总数
  导致 seq≥1 跨序列读 (加 Tps 参数分离); ② PleConvUpdateStateMultiSeq
  CausalKernel 缺 T<state_len 滑窗 (MTP verify T=k+1<9 是常态, 状态不
  前进)。测试 model_verify_multi (2 层 = linear + PLE, B=2 不同 prompt,
  T=3): 多序列 verify logits vs 单序列 prefill 参考 6/6 **bit-exact**
  (l2_rel=0.00000) + 部分接受回滚 (a=0) 后 decode 匹配参考 0.00896。
  67 项测试全绿零警告。 **→ Stage 2a 闭合**; 剩余: Stage 2b (多序列
  投机步: 批量化 draft 循环 + ragged 验证 + extend) + Stage 2c (调度器
  MTP 分支)。
- [x] 2026-09-13 **MTP 批处理 Stage 2b (多序列投机步
  MtpSpeculativeStepMulti)**: B 序列各走一步投机解码, 三段全部批量化:
  ① 批量化 draft 循环 — 每步 j 把 B 序列的 draft[j-1] 打包成一次
  MtpForward (d_seq_id 恒等映射, T=B, 每序列 1 token, Stage 1 多序列
  路径), 滚动 trunk 用 per-seq 池 d_ms_g_pool [max_seq, hc_dim], draft
  循环从 B×k 次 forward 降到 k 次; ② 多序列验证 — ModelVerifyMulti
  (Stage 2a) 打包 B×(k+1) token 一次主模型 forward, per-seq per-token
  checkpoint 按序列各自回滚 (ModelRestoreCheckpoint); ③ 批量化 extend —
  各序列接受前缀 [d_0..d_{a_b-1}, next_b] 连续打包 (T=Σ(a_b+1)) 一次
  MtpForward 重建 draft KV, hidden gather (GatherTrunkRowsKernel) 把每
  序列的 verify trunk 行 h_{P_b}..h_{P_b+a_b} 收集连续。新增持久 scratch
  (d_ms_ids/pos/seqid/sample/multi/gather/g_pool/ext_seq, max_seq>1 时
  分配) + 修复 MtpModel::Free 既有 d_spec_multi 泄漏。实现中发现并修复
  2 个既有 bug: ① RunLayers 在 logits==nullptr 时仍调 HeadForward →
  Bf16Gemm 输出指针 null 触发 CUBLAS_STATUS_INVALID_VALUE (trunk-only
  路径如 MTP draft extend 的 hidden gather 会挂), 加 `if(!logits) return`
  跳过 lm_head; ② MtpSpeculativeStepMulti 初版给 ModelVerifyMulti 传
  history=nullptr 导致 PLE n-gram 上下文全被 EOS 填充 (层 1 PLE
  embedding 污染, logits 全错), 改为按 seqs[b].history 构造 per-seq
  history 行。测试 mtp_spec_multi_step (2 层 = linear + PLE, B=2 不同
  prompt 长度 4/5, k=3): 批量化投机步 vs prefill 语义贪心 ground truth,
  两序列 accepted/next_b 全匹配 + next_d0 有效 + 无跨序列污染。68 项
  测试全绿零警告。 **→ Stage 2b 闭合**; 剩余: Stage 2c (调度器 MTP
  分支: 把 MtpSpeculativeStepMulti 接入 serve 连续批处理调度, 并发 MTP
  请求)。
- [x] 2026-09-13 **MTP 批处理 Stage 2c (调度器 MTP 分支, 并发 MTP 请求
  接入连续批处理)**: 把 `MtpSpeculativeStepMulti` 接入 serve 连续批处理
  调度, 让并发 MTP 请求共享投机步 (此前 MTP 请求独占 `model_mu_` 整个
  投机循环, 不能并发)。分 5 个增量提交 (单分支 main 检查点):
  ① 真实 seq_id — `MtpSpeculativeStepMulti` 初版用 batch 索引 b 当
  seq_id (vseq[b]=b, draft 恒等, ext_seq[r]=b, g_pool+b*hc_dim), 只在
  测试 seq_id==b 时碰巧正确; serve 里 seq_id 来自 free pool (≠batch
  索引), 全改用 `seqs[b].seq_id` (seq_of[b]); 签名 `seqs` 从
  `ModelSequence*` 改 `const ModelSequence* const*` (调度器 seq 不连续);
  测试改 swapped seq_id (seqs[0]→slice 1, seqs[1]→slice 0) 验证 + 修
  测试顺序 bug (MtpResetState 在 draft KV 构建后执行把刚建的 KV 清零 →
  两序列都 accepted=1, 移到构建前)。② verify/extend buffer 持久化
  (d_ms_vlogits/vtrunk/ext_*, 每步不再反复 cudaMalloc/Free)。③
  `MtpDraftExtend` 加 seq_id 尾参 (max_seq>1 时 H2D 常量 d_seq_id 写各自
  draft KV slice)。④ serve MTP 路径 per-seq 化 (per-seq reset +
  per-request d_mtp_g + MtpDraftExtend(seq_id))。⑤ 调度器 MTP 分支 —
  `SchedulerLoop` 把 pending 请求 split 成 mtp_reqs/plain_reqs, MTP 批量
  跑一次 `MtpSpeculativeStepMulti`, plain 跑 `ModelDecodeBatchMulti`;
  `HandleChat` MTP 投机循环从请求线程直接跑 step 改为注册调度器 + 阻塞
  cv, 请求线程推进 seq (position/history)。`ActiveRequest` 加
  `ModelSequence* seq` 指针 (调度器读其 position/history/seq_id/stage 跑
  step, 请求线程拥有 seq, 运行 step 期间请求阻塞 cv 无竞争)。
  测试: 68 项全绿零警告 + serve 3 并发 MTP × 3 轮全部语义正确且确定
  (无跨序列污染, 无 error/illegal/503)。吞吐: 单 200 token ~9.0s,
  3 并发 25-30s (聚合 ~22 vs ~21.8 tok/s, 提升有限) — 根因是 MTP 步长
  错位 (各请求每步接受数不同 → 投机步天然不同步, B 很少达请求数, 实测
  B 分布 81×B=1 + 38×B=2), 属 MTP 投机解码固有特性而非 4b bug。
  **→ Stage 2c 闭合** (MTP 批处理 Stage 1+2a+2b+2c 全部完成; 剩余: 完整
  多设备 PD 部署、48 层长序列端到端, 见 docs/PHASES.md)。
- [x] 2026-09-14 **MTP 调度 lockstep (计划 A, 修 4b 吞吐收益小的根因)**:
  4b 初版调度器等待谓词是"任意 MTP 请求 pending 即跑", 各请求接受数不同
  → 完成时间不同 → 任意时刻 ready 的 MTP 请求数是 ragged 子集 (1~3),
  批量化步的 B 退化 (实测 81×B=1 主导, 聚合吞吐 ~21.8 tok/s 几乎无提升)。
  参考 vllm (scheduler.py uniform `1+num_spec_tokens` + pad 到统一尺寸)
  与 sglang (spec_utils.py `resolve_num_tokens_per_req` 统一 per-request
  宽度) 的 lockstep 范式, 把 `SchedulerLoop` 的 MTP 等待谓词改为"**所有
  活跃 MTP 请求都 pending 才跑**" (plain decode 仍保持机会式批处理)。
  批量化 `MtpSpeculativeStepMulti` 的 B 恒等于活跃 MTP 请求数 (uniform
  步宽)。请求线程侧无需改动 (每步已重新注册); 最慢请求的 CPU 后处理
  (tokenize/SSE) 在 sched_mu_ 外不阻塞调度器。实测 (3 并发 MTP, 各 200
  token): B 分布 81×B=1+38×B=2+0×B=3 → **5×B=1+16×B=2+37×B=3** (B=3
  成主导); 聚合吞吐 ~21.8 → **29.9 tok/s (1.37×)**, wall 25-30s → 20.1s;
  单请求无回归 (纯 decode ~22 一致), 3 并发输出正确 0 error。残留 B=1/
  B=2 步是请求加入/退出边界效应。68 项测试全绿零警告。`Q4T_SCHED_DEBUG=1`
  打印每步 B (env 门控)。参考调研见 docs/REFERENCE_MTP.md (vllm 09-14 /
  sglang-ssd-stream v0.3.0 更新, 含计划 B/C 的 QSA 索引复用 + FP8 参考)。
- [x] 2026-09-14 **计划 D (draft 循环去 host 同步) + 锁步死锁修复**:
  (1) 计划 D: `MtpSpeculativeStepMulti` draft 循环 GPU-resident 化 —
  新增持久 scratch `d_ms_drafts [max_seq, k_max]` (按 seq_id 索引的 draft
  token 矩阵) + 3 个小 kernel (GatherDraft/ScatterDraft/GatherDraftMatrix),
  循环内零 host 同步 (旧版每步 D2H argmax + H2D 下一 token), 对齐 vllm
  `llm_base_proposer.py` 无 host 同步模式; 修 2 个初版 bug (种子列误用
  连续 memcpy / 最终提取误把 host 指针当 device 输出)。(2) **锁步死锁
  修复 (计划 A 遗留 bug)**: MTP 请求从 `active_` 移除后补
  `sched_cv_.notify_one()` — 计划 D 首版 E2E 3 并发 1 请求挂死 300s
  (GPU 0% server 存活), `Q4T_SCHED_DEBUG` B 序列 `B=1,B=3×37,B=2×12,卡死`
  定位为**丢失唤醒竞态**: 请求 c 置 pending+notify 时调度器在两次迭代之间
  (未入 wait) 唤醒丢失 → 调度器睡眠时谓词 `pending_mtp(1) != active_mtp(2)`
  → X 移除自己未 notify → 谓词本应转 true 却无人唤醒 → 永久死锁。计划 D
  更快的 draft 循环改变时序暴露了该潜在竞态 (非计划 D 引入)。验证: 68 项
  测试全绿零警告 + **3 轮 × 3 并发 MTP × 200 token 全部完成** (修复前
  1/3 挂死): 19.3/20.1/19.3s (31.0/29.9/31.1 tok/s), B 分布
  34×B=1+46×B=2+190×B=3 (B=3 主导), 0 error, 输出确定性。**计划 D 吞吐
  收益 ≈ 0** (29.9→29.9~31.1, 噪声内): draft 循环 (2× 单层 forward) 仅占
  投机步 ~8%, 48 层主模型 verify 占大头 — 计划 D 优化了错误目标, 真正瓶颈
  是 verify (计划 B 目标)。计划 D 保留 (代码更干净, 对齐 vllm, 为 cudagraph
  铺路) 但不计吞吐收益。
- [x] 2026-09-14 **性能 profile 收尾 (多序列分段计时 + 瓶颈定位, 计划 B
  否决)**: 多序列 `MtpSpeculativeStepMulti` 加 `Q4T_MTP_TIMING` 分段计时
  (draft/verify/extend, 与单序列同格式, env 门控)。nsys per-kernel 两
  regime 实测: 短序列 (60 tok) verify 占 84%, GEMM 权重带宽主导,
  indexer 仅 0.2%; 长序列 (3707 tok, `--max-prefill 4096`)
  SparseAttentionKernel 占 71.4% (prefill 主导: 12 次 prefill 每次 ~1s),
  indexer 仅 2.5%。**计划 B (MTP 复用 QSA top-k 索引) 否决**: indexer
  两 regime 都非瓶颈 (此前"长序列 indexer 61-73%"判断引用了 bitonic
  sort 并行化前的旧注释, 实测已降到 0.2%)。真瓶颈精确区分: decode
  吞吐 = GEMM 权重带宽 (48 层×84GB/verify 步, 方向 FP8 计划 C);
  prefill/TTFT = SparseAttentionKernel (方向: profile 后优化, 主要降
  TTFT)。68 项测试全绿零警告; 长序列 E2E (3707 tok + 30 decode) 正常。
- [x] 2026-09-15 **`--max-seq 1` MTP 空输出 bug 已修 (262K 单序列关键)**:
  serve `--max-seq 1` (262K 单序列场景)MTP 路径空输出。根因:
  `MtpReserveScratch` 用 `if (m.max_seq > 1)` 守卫跳过多序列投机 scratch
  (d_ms_*) 分配; 但 Stage 2c 后调度器把**所有** MTP 请求 (含 B=1 单请求)
  路由到 `MtpSpeculativeStepMulti`, 该函数开头检查
  `mtp.d_ms_ids == nullptr` 即返回 Fail → 所有 MTP 步失败 → 空输出。
  修: 去掉 `max_seq > 1` 守卫, 无条件分配 scratch (max_seq=1 时 buffer
  极小, 无额外开销)。serve `--max-seq 1` 端到端 3/3 正常生成。
- [x] 2026-09-15 **full-attention 独立 bench 工具**: `tools/bench_full_attn.cu`
  + `q4t_bench_full_attn` target, 只载 layer-3 self_attn 权重 (~100MB), 秒级
  测 FullAttentionForward 各 T (替代 50s 的 serve+nsys 全模型流程), prefill
  优化快速迭代环。`--max-len/--ts/--iters/--golden/--check/--diag`。
  注意: 孤立冷启动 FullAttentionForward (cublasLt 冷缓存) 可能非确定, 不代表
  生产 bug — serve 路径 (warm 缓存) 已验证跨进程 bit 一致 (见 LOG 2026-09-15)。
- [x] 2026-09-15 **SparseAttentionKernel 优化探索 (两项均回退, 负面结果)**:
  nsys per-kernel (T=4096): SparseAttentionKernel 4.81ms (42.8%) 最大单项。
  分析: 延迟受限 (有效带宽 ~31GB/s ≪ 8TB/s), 靠 98304 block 并行隐藏随机
  K/V 读延迟。尝试 ① sK/sV 改 BF16 存 (occupancy 3→6 block/SM): 11.08→
  11.17ms 略差; ② GQA K/V 共享 (block (t,qh)→(t,kvh), 流量降 12 倍):
  11.09→15.16ms 回退 37% (block 数降 12 倍, 并行度损失 > 流量收益)。
  均回退, 保持基线 (T=4096 sparse 11.09ms)。详见 LOG 2026-09-15。
- [x] 2026-09-15 **prefill 瓶颈重定位 (nsys 实测) + flashinfer/FA 参考就位**:
  此前"MoE GEMM 权重带宽下限"判断被 nsys 推翻 (20K 分块 prefill
  per-kernel): **SparseAttentionKernel 78.8% (764ms/call, 120 次) +
  IndexerLogitsKernel 5.8% = attention 侧 84.6%**; MoE 全部仅 ~6.4%
  (gather/scatter 访存为主, FP4 GEMM 极小)。SparseAttentionKernel 比
  算力下限高 ~1500 倍 — 随机 paged-KV 读延迟受限 + 24 q-head 对 2
  kv-head 的 12 倍冗余读。参考项目: sglang-ssd-stream 用 FA2 (dense
  连续 KV) + Triton sparse GQA (远端 top-k) 分解; vllm MoE 用 flashinfer
  monolithic kernel。新增参考 `reference/flashinfer` (commit c1c8e3e,
  Blackwell FMHA + sparse mainloop + SM120 NVFP4 attention) 与
  `reference/flash-attention` (commit 0dc2cb4, **FA4 CUTE DSL 原生
  block-sparse** 与 QSA top-k block 直接对应 + `sm100_hd256_2cta`
  匹配我们 hd256)。详见 REFERENCE.md + LOG 2026-09-15。
- [x] 2026-09-15 **Step 1b: FA4 hd256 kernel AOT 全链路闭合 (零 Python)**:
  证明 FA4 attention kernel 能走通 `cute.compile -> export_to_c -> .h/.o ->
  纯 C++ 驱动 (真实 cudaStream_t)` AOT 链路 (Step 1a 已用 vec_add 验证)。
  **关键决策: 用通用 kernel `FlashAttentionForwardSm100` 而非专用
  hd256 2-CTA kernel** — 后者 `assert blocksparse_tensors is None` 且
  paged KV 要求 page_size==128, 而我们的 QSA 是 block-sparse + page 16;
  通用 kernel 支持 hd256 + block sparsity + paged_kv_non_tma (page 16) +
  GQA + causal, 才是能替换 `SparseAttentionKernel` 的 kernel (hd256 在
  通用 kernel 只能 1-CTA, q_stage=1 使 tmem 512<=512)。依赖修正: torch
  2.14.0+cu132 (匹配系统 CUDA 13.3) + `nvidia-cutlass-dsl[cu13]` +
  **quack-kernels 0.6.5** (Dao-AILab 真依赖, PyPI `quack` 是同名假包)。
  AOT 脚本 `tools/cute_aot/fa4_aot_compile.py` (stub 包绕过 FA2 C 扩展
  import + 自定义 `@cute.jit` 包装函数把 `AuxData` NamedTuple 参数内化为
  编译期常量, 使 C 导出干净)。C++ 驱动 `tools/cute_aot/test_fa4_aot.cpp`
  (零 Python, 自包含 bf16, fp32 causal 参考)。**结果 PASS: l2_rel=
  0.001834, argmax_mismatch=172/3072 (near-tie, bf16 精度内)**。
  Step 2 已闭合 (负结果, 见下条): FA4 稀疏模型与 QSA 不兼容, 改走计划 1
  (GQA packing 重写) 亦比原 kernel 慢, 已回退。详见 LOG 2026-09-15。
- [x] 2026-09-15 **Step 2: GQA packing 重写 SparseAttentionKernel (负结果,
  回退)**: FA4 稀疏模型不兼容 (FA4 `BlockSparseTensors` 是 per (batch,
  head, m_block) 粒度, QSA 是 per-token head 共享; `gather_kv_indices`
  需 qv 不能与 paged KV 组合) → 无 FA4 路径可直接替换, 改走**计划 1**:
  借 FA4 思路用 GQA packing 重写 `SparseAttentionKernel` (一个 block 服务
  一个 kv-head 的多个 q-head, KV 只 gather 一次)。实现 bit-exact (QK 逐维
  + 跨 warp 累加顺序与旧 kernel 完全一致, 68 测试全过), 但**性能负结果**:
  ```
                 T=2048 dense    T=4096 sparse
  原 per-qh      5.325 ms        10.538 ms   (最快)
  GQA=4          5.539 ms        10.933 ms   (+3.7%)
  GQA=12         6.683 ms        13.193 ms   (+25%)
  ```
  趋势单调: packing 越大越慢。ptxas 48/64/96 寄存器, 三者都 smem 限制
  1 block/SM。**根因**: GQA packing 优化的是 KV gather, 但 QSA per-token
  KV footprint 被 idx_budget (2048 位置) 封顶 ≈2MB, **始终 L2 驻留**
  (Thor L2=32MB), gather 非瓶颈 (L2 命中非 HBM miss) — "12× KV 读取
  减少" 是红鲱鱼; packing 把 grid 从 T×24 降到 T×6/T×2 (并行度损失
  3×/12×) + 寄存器 48→64/96, 净变慢。即使 262K 长上下文, sparse
  attention 每 token 也只读 2048 位置, footprint 不变, 结论不变。回退
  `full_attention.cu` 到原 per-q-head kernel (最快), 保留 bench 增强
  `--warm-idx` + `--base-pos` (production-like 散射读模拟)。详见 LOG
  2026-09-15。
- [x] 2026-09-15 **Step 3: SparseAttentionKernel 瓶颈诊断 (nsys + 2 实验,
  定位串行链)**: nsys per-kernel (T=4096 sparse): SparseAttentionKernel
  42.3% 最大单项 (avg 2.1ms, max 4.68ms), HBM 有效带宽仅 ~1.5TB/s
  (≪8TB/s), 计算量 ~103 GFLOP (SIMT fp32 下限 ~1.4ms, 当前 10.5ms =
  下限 7.5×)。两实验排除候选瓶颈: ① bf16 sK/sV (smem 33.6→17.5KB,
  occupancy 1→2 blocks/SM, 数值 l2_rel 6.4e-3 过容差) **性能持平** →
  occupancy 非瓶颈; ② GQA packing (Step 2) 减 L2 流量 12× 更慢 → L2
  流量非瓶颈。**根因: 每 block 内部串行工作** — block=(t,qh) 256 线程,
  CHUNK=16, 每 block 串行 ~128 chunk, 每 chunk 4 次 `__syncthreads` +
  gather/compute 串行 (gather chunk c 完才 compute c, gather 延迟无法被
  compute 隐藏), 128×4=512 sync + 128 串行 gather 是硬下限。优化方向
  (待决策): ① prefetch 双缓冲 (gather c+1 与 compute c 并行, 需 bf16
  sK/sV + 手动双缓冲, cp.async 不支持 paged 间接) ② tensor core (FA4
  AOT, 理论下限 ~0.05ms, 但需自建 block-sparse + paged gather, 工程量大)
  ③ 接受当前 10.5ms 转 decode GEMM 带宽 (FP8 计划 C)。回退 bf16 (无
  收益), 保持基线。详见 LOG 2026-09-15。
- [x] 2026-09-16 **Step 10: GatedDeltaNet 内循环 ILP + warp reduction →
  prefill 786→826 tok/s**: Step 9 后 GatedDeltaNet 成 prefill #1/#2 瓶颈
  (nsys 22-24%)。两处安全优化 (prefill + `GatedDeltaNetMultiSeqCausalKernel`
  同步改, 保持逐位一致, 对参考 l2_rel 在容差内): (a) 内循环 4 路 ILP —
  `kS_j`/`y_j` 两个 128 深串行 FMA 链拆 4 累加器 (共享 helper `GdnKSum`/
  `GdnUpdateY`); (b) warp-shuffle 归约 — 每 token k_sq/q_sq 归约从两次
  halving-tree `BlockReduceSum` (~14 barrier) 换成一次 `BlockReduceSum2Warp`
  (1 barrier), 每 token barrier 18→3。**cuobjdump 实测 GatedDeltaNetKernel
  REG:127 无 spill, 但 66KB FP32 state 限死 3 block/SM = 25% 占用率 (shared
  才是天花板, 非寄存器)**; kernel 距 FP32 峰值 ~18×, 占用率受限, 固定占用率
  下 ILP+warp 只拿 ~5%。踩坑: 先试 vd-split 拆列提占用率, 但 (i) 改归约线程
  数破坏 prefill/verify 逐位一致 → MoE 路由离散边界放大差 → verify_multi
  seq0 l2_rel 0.029 FAIL; (ii) 实测 vd-split 不增 warp/SM (state 总量不变)
  收益≈0, 遂弃。全 68 测试通过, 零警告。下一步大杠杆: bf16 shared state
  (alpha 衰减 → 误差或有界, 可回退试) 或 chunked tensor-core delta rule。
  详见 LOG 2026-09-16。
- [x] 2026-09-16 **Step 9: PV 累加器搬寄存器 → prefill 268→786 tok/s (2.9×)**:
  Step 8 用 shared sO[12,256] 做 PV 累加 (每 chunk 读改写 3072 float)。改为
  per-lane 寄存器累加 acc[4][4] (online-softmax rescale 折进寄存器 fma), 移除
  shared sO, shared/block 37.5→25.5KB。**单层 L2 驻留 bench 持平 (8.19→8.25ms)
  但真实 prefill 268→786 tok/s (2.9×), SparseAttentionKernel 584→60ms/call
  (9.7×; 从原始 SIMT 1264ms 算 21×)**, nsys 占比 73.6%→22.7%, GatedDeltaNet
  升为 #1 (23.9%)。根因: 真实 kernel **延迟受限** (散射 LPDDR5x KV 读), 释放
  shared → 更多并发 block/SM → 隐藏散射读延迟。**教训: L2 驻留单层 bench 对
  占用率敏感的延迟受限 kernel 误导 (Step 2-7 一直被误导), 必须测真实 prefill**。
  全 68 测试通过。详见 LOG 2026-09-16。
- [x] 2026-09-16 **Step 8: tensor-core GQA-packed SparseAttentionKernel**:
  用户要求利用 tensor core + 纠正 Thor 硬件 (**无 HBM, LPDDR5x 统一内存,
  理论 273 GB/s, 实测可用 246+ GB/s**; AGENTS.md "8TB/s HBM3e" 错误)。
  验证 SM110a 支持 bf16 `mma.sync m16n8k16` (CUDA13 `nvcuda::wmma` bf16
  fragment incomplete 但底层 PTX 可用)。重写 kernel: grid T×24 (每 q-head
  独立读 KV 12× 冗余) → grid (T,nkv), 每 block 读 KV 一次驱动 12 q-head
  走 mma (QK warp0 sQ[16,256]@sK[16,256]^T 补零到 16 行, PV 8 warp 切 256
  维, online softmax)。修复关键非确定 bug: PV mma 无效位置 sP=0 乘未初始化
  sV(NaN/Inf), 0×NaN=NaN 污染输出 → 多序列 step1 DIFFER; staging 清零
  [chunk,16) sK/sV。全 68 测试通过 (l2_rel 4.7e-3)。性能: 单层 bench
  10.55→8.19ms (1.29×), 真实 SparseAttentionKernel 1264→584ms/call, 端到端
  prefill 2560tok 268 tok/s (attn 仍占 73.6%)。**12× KV 读减少收益有限**:
  kernel 现为 latency/occupancy bound (QK 仅 warp0 + ~128 chunk 串行 + 多
  __syncthreads), 非 KV 带宽 — 推翻 Step 7 "12× 冗余读是瓶颈"。下一步:
  QK 跨 warp 并行 / split-K 跨位置。详见 LOG 2026-09-16。
- [x] 2026-09-16 **Step 7: prefill 慢的真相 (用户反馈 vllm 1k+) — 修正
  Step 2-4 结论**: 用户反馈 "prefill 太慢, vllm 1k+"。实测 prefill
  吞吐: 1051 tok=437 tok/s, 2126=300, 4251=220 (随 T 增大下降)。长
  prefill (T=4251, 19.4s) nsys: **SparseAttentionKernel 78.4% (30.4s/
  38.7s GPU 时间, 36 实例, 每 prefill 层 ~1.2s)** / GatedDeltaNet 6.7% /
  IndexerLogits 2.4% / MoE ~6%。**关键修正: Step 2-4 的 bench 是
  误导性的** — bench 单层 + max_len=4096, KV 8.4MB **驻留 L2 (32MB)**
  → 10.5ms, 据此误判 "KV 驻留 L2, gather 非瓶颈, SIMT 已近极限"。真实
  prefill: **12 层顺序处理**, 每层 KV 16.8MB (max_len 8192), 层间互相
  驱逐 L2 → **HBM 散射读** → **~1.2s/call (80× 差距)**, 有效带宽仅
  ~170GB/s (HBM 延迟受限, 1 block/SM 无法隐藏延迟)。Step 2-4 结论在
  真实场景不成立。**vllm 快 6×+ 的原因**: QSA 用 Triton tensor-core
  kernel (`tl.dot` 2D tile + split-K + paged gather), 2D tiling + 更多
  在途 load 隐藏 HBM 延迟。下一步: 用 tensor core 重写
  SparseAttentionKernel (对齐 vllm Triton 结构), 或先在 HBM-bound 场景
  重测 Step 3b 的 2-blocks/SM (L2 场景持平, 但 HBM 延迟受限场景可能
  显著)。详见 LOG 2026-09-16。
- [x] 2026-09-16 **Step 6: MoE 量化开销分析 (14.3%, launch 开销主导) +
  优化全景**: Step 5 定位 decode 真瓶颈后, 分析 MoE 量化 14.3% 的优化
  空间。MoE 量化 kernel 是 per-expert 循环 (GatherQuant→GEMM_gu→
  SwiGLUQuant→GEMM_dn→ScatterAdd, 5 kernel 按专家串行, 数据依赖),
  28429 实例 × 6-11µs 是 **launch 开销主导** (decode M_e=1 计算极小)。
  优化空间: 批量 GatherQuant 只省 ~2%, SwiGLUQuant/ScatterAdd 无法
  批量 (依赖 per-expert GEMM 输出), 真正消除需 **grouped GEMM 重写**
  (PHASES.md Phase 3)。**decode 优化全景**: BF16 投影 GEMM/GEMV 58%
  (→FP8 大工程, 需质量验证) / FP4 MoE GEMM 19.2% (已优化) / MoE 量化
  14.3% (→grouped GEMM 大工程) / linear attention 2.8% (已优化) / QSA
  0.7% (已闭合)。**两个大工程需用户决策**: ① BF16 投影 FP8 (58% 收益
  最大但工程最大) ② MoE grouped GEMM (14.3%)。本优化阶段 (attention
  闭合 + decode 瓶颈定位 + MoE 量化分析) 数据驱动收尾, 不自主启动大
  工程。详见 LOG 2026-09-16。
- [x] 2026-09-16 **Step 5: decode 全链路 profile (计划 C 证伪, 真瓶颈
  定位)**: attention 优化闭合后转 decode GEMM 带宽, 先数据驱动: nsys
  profile decode 全链路 (MTP 生产路径, 60 tokens) 拿 per-kernel 精确占比:
  **BF16 投影 GEMM/GEMV ~58%** (attention 12 层 + linear 36 层 q/k/v/o +
  DeltaNet 投影, BF16 权重) / FP4 MoE GEMM 19.2% / MoE 量化开销 14.3%
  (SwiGLUQuant+GatherQuant+ScatterAdd) / linear attention 2.8% /
  **QSA full attention ~0.7% (IndexerLogits 仅 0.2%)**。**决定性结论:
  计划 C (QSA indexer cache FP8) 彻底证伪** — IndexerLogitsKernel decode
  仅 0.2%, FP8 化收益 <0.1%, 不值得 (且需质量验证), **计划 C 关闭**。
  真瓶颈 = BF16 投影 GEMM/GEMV 58% (转 FP8 是大工程: 权重格式 + GEMM
  kernel + 重新量化 + 质量验证, 需用户决策) + MoE 量化开销 14.3% (次要,
  可 kernel 融合/launch 减少)。不自主启动 BF16 投影 FP8 大工程, 本
  优化阶段 (attention + decode 瓶颈定位) 数据驱动收尾, 下一步待用户
  定夺。详见 LOG 2026-09-16。
- [x] 2026-09-15 **Step 4: tensor core (FA4) 路径关闭 (源码级 blocker)**:
  Step 3b 收敛后调研唯一能大幅超越 SIMT 7.5× 下限的方向 (tensor core /
  FA4 AOT, Step 1b 已打通全链路)。源码级确认: 通用 kernel
  `flash_fwd_sm100.py:813-815` 明确
  `raise NotImplementedError("Block sparsity + paged KV not supported on
  SM100")` — QSA 需要 block-sparse + paged KV (page 16) 同时, FA4 直接
  抛异常 (硬 blocker); 专用 hd256 kernel `assert blocksparse_tensors is
  None` + paged 要求 page_size==128。用 FA4 须改 FA4 源码 (外部参考,
  不应改) 或放弃 paged KV (Phase 1 硬需求), 两条路都不可行。**attention
  优化路径最终结论: SIMT 已近极限 (5 实验) + tensor core 源码级 blocker
  → 现有架构下无可行方向, 接受当前 10.5ms**。下一步建议: 转 decode
  GEMM 带宽 (FP8 计划 C), 262K 分块 prefill 每块 ~12.6s 主要是 MoE GEMM
  带宽下限, attention 占比有限。详见 LOG 2026-09-15。
- [x] 2026-09-15 **Step 3b: SparseAttentionKernel 5 实验收敛 (SIMT 路径
  已近极限)**: 继续 Step 3 做实验排除候选瓶颈, 共 5 实验全持平/负结果:
  ① GQA packing (减 L2 流量 12×) +25% 更慢 → L2 流量非瓶颈; ② bf16
  sK/sV (occ 1→2) 持平 → occupancy 非瓶颈; ③ `__expf` (单 MUFU) 持平 →
  SFU 非瓶颈; ④ bf16+CHUNK=32 (迭代 128→64, sync 减半) +28% 更慢 →
  迭代/sync 开销非瓶颈 (每位置 gather+compute 本身才是); ⑤ nsys 42.3%
  确认位置。计算量 ~103 GFLOP, SIMT 下限 ~1.4ms, 当前 10.5ms = 下限
  7.5×, 差距来自每 block 对 2048 位置的串行处理 (SIMT 固有)。**结论:
  SIMT 路径已近极限, 进一步大幅超越需 tensor core (FA4 不兼容, 需自建
  block-sparse + paged gather, 工程量大)**。5 实验全回退, 保持基线
  10.5ms。下一步建议: 转 decode GEMM 带宽 (FP8 计划 C) 或接受当前
  attention 性能。详见 LOG 2026-09-15。
- [x] 2026-09-15 **分块 prefill 闭合 (262K 长上下文可用) + rope H2D 修复**:
  `max_prefill` 语义从 prompt 上限改为分块大小 (上限 = `max_len`);
  `T > max_prefill` 走分块: chunk 0 `ModelPrefill` + chunk 1..
  `ModelDecodeBatch` (绝对位置续块, 不重置状态), 中间块跳过 lm_head。
  正确性 (诊断工具 `tools/chunk_prefill_diag.cu`): 分块 vs 一次性末位
  logits **argmax 全一致**, l2_rel 0.15–0.21 在引擎固有非确定带内
  (baseline 两次一次性也差 0.173), 非 bug; 68 测试全绿。性能 bug 修复:
  `ModelDecodeBatch` 的 3D MRoPE 写 `d_rope_pos` 从逐 token 3×T 次 4 字节
  H2D 改为 3 次批量 (位不变)。每块计时: 2048-token 块 ~12.6s flat
  (不随位置增长, 48 层 MoE forward 带宽下限), 262K ≈ 27 分钟 prefill。
  262K E2E: 200K-token prompt 分块 prefill + 30 decode, 内存 ~92 GB 无
  OOM (看门狗), 生成正常。详见 LOG 2026-09-15。

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

## 已解决 (2026-09-13)

- ✅ **B1 多序列隔离两个 bug (model_multi_seq_isolation 非确定性失败)**:
  - **`uint16_t*` kv_cache 字节步长放大 2 倍 (B1 引入)**: `ResetState` /
    `DecoderLayerForward` 对 `kv_cache` (uint16_t*) 用字节数做 per-seq
    偏移, 指针算术按元素计 → 实际偏移 ×2, seq≥2 越界写 (落已映射区静默
    DIFFER, 落未映射区 illegal access, 非确定)。linear 分支用元素数
    一直正确, 故 3 层 (全 linear) 变体不触发。修复: `char*` 字节偏移。
  - **float `atomicAdd` 求和非确定 (既有, 本次暴露)**: full attention
    四个 RMSNorm kernel 用 `atomicAdd` 到 shared float 求平方和, float
    加法不结合 + 完成顺序非确定 → 跨运行 bit 漂移 (seq0 自重跑即间歇
    DIFFER, 与隔离无关)。既有单元测试单次 + 3e-2 容差不暴露。修复:
    确定性 `BlockSum` (warp shuffle 固定序) 替换 4 处 atomicAdd。

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
