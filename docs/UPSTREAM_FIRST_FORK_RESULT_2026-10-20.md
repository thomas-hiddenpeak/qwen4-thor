# 真实文本首分叉判定（2026-10-20）

结论：**本夹具的首个已覆盖算子边界分叉由形状切换后的累加舍入路径
解释，未发现该首分叉属于q4t实现缺陷。** 起点是layer0、token位置74
（四行中的row2）的`linear_attn.in_proj_a`，head43。不是layer2 MoE
内部首先产生的差异。此判定有具体路径复现和单标量干预支持，不是
因差异小、相邻BF16、历史类似或语义分数正常而接受。

## 实际数值与算术路径

输入X与实际W逐位相同；M=1使用生产GEMV，M=4使用cuBLASLt。

| 参考/路径 | 转BF16前的数值 | BF16结果 |
|---|---:|---:|
| 整数dyadic精确点积 | 21541987993 / 8589934592 ≈ 2.5078174649970606 | 最近偶数舍入2.515625 |
| GEMV实测与逐FMA/归约参考 | 2.507817268371582 | 2.515625（0x4021） |
| cuBLAS float输出与独立WMMA | 2.5078110694885254 | 2.5（0x4020） |
| 相邻BF16中点 | 2.5078125 | — |

精确点积比中点高约4.965e-6；GEMV与tensor路径的FP32值相差约
6.199e-6，分别落在中点两侧，最终相差一个BF16间隔0.015625。
此元素T1符合精确实数点积的一次舍入，T4不符合；但生产合同是
BF16输入、FP32累加后转BF16，不要求等于无限精度点积的一次舍入。
不得据此把T1推广为所有点积的正确oracle。

证据链：

1. [原捕获](UPSTREAM_FIRST_FORK_CAPTURE_2026-10-19.md)425边界有效，
   旧layer2自然前向控制逐位重现。layer0此前HC及qkv/z投影一致，
   beta全部一致，a只有该元素不同。两次独立prefill起始状态一致。
2. [局部投影](UPSTREAM_LOCAL_REPLAY_2026-10-20.md)192个T1/T4输出
   逐位复现；四行head43的GEMV FP32由独立有理数逐FMA、加法、
   shuffle归约舍入逐位复现。并非仅与一个宽误差包络比较。
3. [实际CUDA trace](evidence/upstream-cublas-trace-20261020/kernels.json)
   记录两种CUTLASS WMMA kernel；原6调用全部输出未变。算法配置
   字段相同本身不足以证明路径相同，原BF16算法对float布局的
   `INVALID_VALUE=7`仍保留，不把失败后的初始化state=0当通过。
4. [CUPTI捕获](evidence/upstream-cublas-modules-20261020/capture.log)
   取得实际加载的两个SM110 cubin，共4,557,400字节，输出不变。
   [SASS审计](evidence/upstream-cublas-modules-20261020/sass-audit.json)
   表明前317条地址/指令/操作数完全一致，含全部8条静态
   `HMMA.16816.F32.BF16`及主循环回边。首次不同在0x13d0的输出阶段。
   比较不包含调度控制位，也不把不同输出布局的全部参数结构宣称相同。
5. [独立WMMA](evidence/upstream-wmma-20261020/comparison.json)不引用
   q4t/cuBLAS实现。直接按K递增每16项累加，全部192个FP32逐位等于
   cuBLAS float输出，host精确nearest-even转BF16后全部逐位等于原
   BF16输出。全零、四个K边界稀疏基向量和补零行控制通过。
   这排除了本例结果必须依赖q4t/cuBLAS错误索引、缩放或输出转换
   才能产生的解释；该参考独立于软件实现，仍使用同一GPU tensor硬件。

NVIDIA [PTX WMMA数值合同](https://docs.nvidia.com/cuda/parallel-thread-execution/#warp-level-matrix-instructions-wmma-mma)
规定BF16矩阵操作的中间累加至少为单精度，未指定累加顺序、舍入及
subnormal处理。实际内核已确认使用BF16/FP32 tensor指令，独立WMMA
已重现此次结果；因此这里的结论不只依赖通用规范。没有反推出或
声称证明硬件内部未公开的逐项加法树，也不把WMMA解释成逐项FP32 RNE FMA。

## 更早状态与因果范围

[30项GDN检查](evidence/upstream-gdn-20261020/run.json)复用不改动的
生产kernel，实际5个kernel指令体与生产一致。前缀1/2完整FP32 SSM、
BF16 conv状态及qkv_norm/raw_y/gated均与T1前缀逐位一致，排除本次
layer0在位置72/73已有隐藏GDN状态分叉。未开启会改变kernel分派的
checkpoint参数。保留T4其他输入，仅把a[2,43]由0x4020替换为0x4021，
四行GDN输出及最终完整状态即与T1逐位一致。

所以该a投影是本次layer0 GDN差异的直接原因；差异随后进入HC/MoE
及层1。原layer2 MoE同输入对照逐位一致。本结论定位到算子及标量，
不声称已找到tensor硬件内第一条发生舍入的微操作，也不声称所有
下游差异都只有这一来源；后续层仍可能产生自身跨形状差异。

## 与历史记录的关系

| 记录 | 当时发现 | 与本次关系 |
|---|---|---|
| [10-09 HC](MTP_STRICT_BASELINE_2026-10-09.md) | 相同normed后down[81]为-12.3125/-12.25，传播到SiLU/up/mixed；未证明内部归约树 | 同属跨形状投影差异先例，但本次此前HC相同，不能沿用其首分叉或未完成解释 |
| [10-16 beta](evidence/mtp-numerical-closure-20261016/phase01/README.md) | layer0 beta[41]相差1 BF16 ULP，精确点积距中点约7.58e-7，prefill侧并非更准确 | 最接近本次的现象；本次是不同文本、a[43]，已额外完成实际内核、独立WMMA和状态干预证据 |
| [10-16 PLE缺陷](MTP_NUMERICAL_CLOSURE_2026-10-16.md) | pinned staging异步复用导致160/160错误，事件所有权修复并验收 | 真正实现缺陷先例；与本次同输入局部投影可由独立tensor路径复现不同，不能由舍入先例排除竞态 |

## 复算与交付边界

原始目录（均保留，模型权重不入Git）：

- `.q4t-work/upstream-first-fork-20261019/gpu-v1`：全模型捕获。
- `.q4t-work/upstream-projection-20261020/gpu-v1`：6调用投影及GEMV参考。
- `.q4t-work/upstream-gdn-20261020/gpu-v1`：8次状态pipeline、30项检查。
- `.q4t-work/upstream-cublas-trace-20261020/run-v1`：原binary六调用trace。
- `.q4t-work/upstream-cublas-modules-20261020/run-v1`：原binary六调用模块捕获。
- `.q4t-work/upstream-wmma-20261020/run-v1`：3次独立WMMA及控制。

后三阶段共36个manifest文件摘要复核通过，lease均COMPLETED、子进程0；
每阶段完整持锁，无自动重试，无新增全模型加载/HTTP。轮询不保证发现
短于采样间隔的非合作干扰。静态库sm110/sm80提取未命中及PTX尝试
保留在trace父目录，不删除失败尝试、不将其作为最终内核证据。
实际cubin及SASS留在本机；Git保留摘要、清单、运行与比较报告。

新增工具仅独立诊断；生产src/include无修改，默认build/q4t摘要仍为
`44abd8b1a7edaca41620b87b4cf894427f8397c1e3529ef56512940ef1b44e03`。
未做运行时修复、性能变更或全模型质量接受。本次目标完成为“该固定文本
首分叉定位与分类”，不等于所有文本、形状、后续层或任务质量均已通过。
