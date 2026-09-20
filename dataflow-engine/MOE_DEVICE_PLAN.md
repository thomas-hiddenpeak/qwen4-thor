# MoE 单 token 的 GPU 执行合同

2026-09-21。原设计基于 693c3ee 的源码审阅，已在现有 runner 中实现。
最终产物完整 E2E、数值与 HTTP 时间线通过，已接受并更新正式参考。
实现边界、容量、实测与限制见
[MoE 设备执行报告](../docs/MOE_DEVICE_DECODE_2026-09-21.md)。
本文以下保留设计时的依赖分析与合同，不作为最终产物已验收的声明。

## 原路径事实与需要移除的依赖

`src/quant/moe_gemm.cu::MoERoutedForward` 先在 GPU 建立 counts 与
`token_list[E,M]`，将 counts 回传 host 并同步。host 再决定非空专家、
每专家 `M_e`、输出 prefix offset、四条 stream 的分配、GEMM 参数。
随后上传 offset、构建逆映射，按专家执行 quant→GU→SwiGLU/quant→DN，
最后回到调用 stream，按原 top-k slot 顺序确定性合并。

因此，删除 counts 回传会使现有 host GEMM 循环失去必要输入；单纯
合并分配或改成 CUDA Graph 不会自动消除这条依赖。此前已接受分配
合并降低了调用数，但仍保留每步 48 次 counts 回传。

数值必须保留：专家各自的 input_scale；GU 与 DN 各自的 FP32 alpha；
GU 的 BF16 输出舍入；原 SwiGLU 表达式；每 16 值量化、E4M3 scale
舍入及 swizzle；DN 的 BF16 输出；最终按 slot 顺序乘 router 权重累加。
不把 alpha 移到 BF16 舍入之后，不共享不同专家的量化结果，不混入 FP8。

## 已核对的库接口

本机头文件是 cuBLAS **13.5.1**，q4t 链接 CUDA Toolkit **13.3** 目录
中的 libcublasLt.so.13。Toolkit 与库的版本号不同。

本机 cublasLt.h 提供 grouped layout、device shape/pointer arrays、
per-batch alpha/beta。官方发布记录将 NVFP4 grouped 支持列在 Toolkit
13.2 Update 1，覆盖 CC 10.x/11.0、MMA K=64；本模型 K=2560/640
满足这一整除要求。NVIDIA Transformer Engine 的 grouped 实现也提供
NVFP4 scale 指针数组，要求 cuBLAS 13.4+。这些是实现候选的依据，
不证明本机具体形状有可用算法、数值一致或性能更快。

来源：[CUDA 发布记录](https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html#cublas-release-13-2-update-1)、
[grouped layout API](https://docs.nvidia.com/cuda/archive/13.3.0/cublas/index.html#cublasltgroupedmatrixlayoutcreate)、
[NVIDIA 实现](https://github.com/NVIDIA/TransformerEngine/blob/main/transformer_engine/common/gemm/cublaslt_grouped_gemm.cu)。
本机源码/头文件 SHA 与链接路径保存于
`.q4t-work/e2e/moe-device-plan-20260921/sources.json`。
没有运行库能力 probe、微测试或 benchmark。

## 第一阶段的固定形状

限定 M=1、E=512、top-k=10、hidden_size=2560、moe_is=640。
其他形状沿用当前路径；prefill、批量和 MTP 的扩大另行验收。
这不是宣称 MoE 全部 GPU 化，而是先闭合普通单 token 的完整专家链。

对于这个形状，GPU 已有十个 expert_ids 和 router_w。合法 router
输出的十个专家互异，因此每个 assignment 都是一个 M_e=1 group。
直接以 slot 为 group，数量恒为 10，不需要 host 活跃数、prefix sum、
E×M token_list 或 row_of_flat。两个矩阵分别为：

| 投影 | activation | weight（每个选中专家） | BF16 output |
|---|---|---|---|
| GU | [1,2560] NVFP4 | [1280,2560] NVFP4 | [1,1280] |
| DN | [1,640] NVFP4 | [2560,640] NVFP4 | [1,2560] |

cuBLAS 的列主序描述必须沿用当前 W 作为 A、activation 作为 B 的转置
约定。不是把原 row-major 参数直接塞给 grouped API。

## 一次 forward 的依赖图

1. **准备和输入量化**：读取 device expert_ids；产生十组权重/scale/
   activation/output 指针及 alpha，按 slot 写入各自的量化区域。
2. **grouped GU**：一次 API 调用覆盖十组，输出按 slot 排列。
3. **SwiGLU 与 DN 量化**：保持 GU BF16 舍入边界、每专家 DN scale，
   准备 DN 描述符，允许复用已经消费完的量化 payload/scale 区域。
4. **grouped DN**：一次 API 调用覆盖十组，输出仍按 slot 排列。
5. **固定 slot combine**：按 slot=0..9 原序累加进已有 FP32 y。

所有节点先放调用方同一 stream，用正常 kernel/GEMM 边界保证可见性。
初版不引入跨 CTA 自旋、PDL、额外 stream 或融合掉 BF16 中间舍入。
动态数据不回传 host。host 仍提交固定数量的节点，这不等于 persistent
kernel，也不等于整个模型没有 host 边界。

## 容量和生命周期预算

数据字节采用本项目 SfBufferSize；数字是推导，尚未实测峰值。

| 对象 | 十个 group 的需求 | 消费结束 |
|---|---:|---|
| GU activation payload | 10×1280 = 12800 B | GU 完成 |
| GU activation scale | 10×20480 = 204800 B | GU 完成 |
| GU BF16 output | 10×2560 = 25600 B | SwiGLU 完成 |
| DN activation payload | 10×320 = 3200 B | DN 完成 |
| DN activation scale | 10×5120 = 51200 B | DN 完成 |
| DN BF16 output | 10×5120 = 51200 B | combine 完成 |
| 指针/shape/alpha | 按字段对齐独立计入 | 对应 grouped GEMM 完成 |

scale 是十个独立的完整 swizzle atom，不能将 SfBufferSize(10,K)
当作十份 SfBufferSize(1,K)。当前 M=1 workspace 的 scale 区域仅
20480 B，若独立并发十组，最低多出 184320 B 的 scale 容量。
如果保持原 workspace 不动，另租 204800 B scale 表，应按整块新增
成本计账，不能把未真正释放的旧区当作已经省下。

payload/scale 可按最大 GU 容量复用，DN 必须使用明确 slot stride，
不是默认紧密排列。GU/DN 输出可复用已有 workspace 中不重叠的区域。
所有指针数组满足库的对齐约定；shape 整数宽度必须显式匹配布局。
临时描述符及量化区由本次调用持有，直到 combine 后同流释放；缓存
的库计划只缓存 shape/算法等稳定内容，不能保存已释放临时区的所有权。

## 候选实现与验收次序

先在独立 CUDA 编译单元提供固定形状入口，原入口只做形状分派，
避免改变旧 prefill 编译单元的 device 实现。编译和模型内首个请求
可发现库不支持；不在前面运行独立 GEMM probe。

首项测试必须是真实 EvalScope HTTP 质量 E2E，随后完整五档性能。
必须记录新分支是否实际执行、是否找到 grouped 算法。若无算法，
记录失败并撤回/修复；不能静默回退后把旧路径的通过当成新方案通过。
保持模型目录和 reference 只读，不升级系统 CUDA 来绕过首轮结果。

全部 E2E 通过后才比较中间量化 payload/scale、GU/DN BF16 和最终
FP32 combine；新 grouped 算法的数值变化不能仅用“量化噪声”解释。
之后在 HTTP 时间线核查 counts 回传和逐专家 host 提交是否消失、
GEMM 与量化/合并总成本是否下降、是否增加 workspace 或隐藏同步。

理论上每层 20 次 GEMM API 调用降为 2 次，48 层是 960→96；这不是
CUDA kernel 次数保证，也不是按比例加速预测。不得把旧同步 API 的
等待时间直接当作可节省时间，其中可能包含必要 GPU 计算。

长期仍需从 M=1 推广到 ragged prefill/批量，明确 padded rows、零行
专家、容量及释放边界；本阶段不把 compute-all-512 或 M=1 的固定形状
称为最终通用执行计划。
