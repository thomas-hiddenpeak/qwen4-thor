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
  待补: serve 层视频接入 (video token 248057 + 多帧)。
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
