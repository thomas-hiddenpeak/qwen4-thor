# 从 runner 演进 GRFrame 的第一条执行合同

2026-09-21。第一阶段已接受（基线 fcb5925），完整 HTTP、数值与时间线通过。

## 改动前调用链

DecoderLayerForward 对 attention 与 MLP 分别执行：
HyperConnectionMix(R) → mixed、normed → 子层(mixed) → block_output →
HyperConnectionCombine(block_output,R,normed)。Combine 此时才用 normed
做 block_inject 投影、BF16 舍入与 gate，然后写回各 HC 分支。

normed 不参与子层计算，却因 inject 推迟而跨越整个子层存活。
源码名 d_res_a/d_res_m 实际对应归一化张量，并非原始 residual；
真正保留的 residual 是 d_trunk / d_combined。两份 normed 的使用区间
不重叠，当前各自从 decoder workspace 取得独立区域。

每份 normed 为 T*4*2560*2 字节：T=8192 时 160 MiB，T=1 时
20 KiB。两份共 320 MiB 是现有区域容量，不是可直接宣称的峰值节省。
即使 inject 提前，GRRead 内仍需临时 normed；若不复用其空闲区，
提前释放生命周期并不自动降低物理 arena 容量。

## 数值与所有权合同

提案 GRRead(R) 输出 mixed 与 frame：frame 含 residual 的只读借用、
frame 拥有的 BF16 inject_gate[T,4]、T/形状及有效性，不拥有 normed。
GRWrite(block_output,frame) 沿原 CombineWithGate 写出结果。

保留 grouped RMSNorm、down/silu/up、mix 的当前精度和次序；inject
投影仍先 BF16 舍入，再 FP32 gate 运算，再 BF16。T=1 复用已验收的
HcInjectGevGated，其他 T 保留现有 Bf16Gemm+ApplyInjectGate。
不在第一步拼接 down/inject 权重或改变 GEMM 归约方式。

源码 SiluDivKernel 的准确顺序是读取 BF16 down→FP32 乘 inv_hc→
SiLU→BF16，再送入 up 投影；即 SiLU(down/hc)，不是 SiLU(down)/hc。
HyperConnectionMix 调用处的后者注释不准确，不能作为重构数学依据。
当前 kernel 计算不修改；这一点须进入后续中间值对照。

frame 生命周期到 GRWrite 完成；原 residual 不能在此之前覆盖。
同 stream 的 kernel 顺序约束 GRRead→子层→GRWrite；若子层有辅助
stream，必须沿现有汇合依赖完成其最后消费者，不能只凭 host 返回
就复用内存。frame 不等于 MTP checkpoint，也不增加序列状态。

## 分阶段落地

1. 先引入明确的 read/write 边界，将 inject 提前，保留原大缓冲布局，
   隔离调度顺序影响。旧 API 的 head mixer 与 MTP 调用者继续有效；
   use_combine=false 的 mixer 不能强制访问不存在的 block_inject。
2. 单独核对最后消费者后，合并两份 normed 的非重叠生命周期；同步
   修改 workspace 预算与实际 carve，不能只改指针或只减预算。
3. 只有 read 阶段临时 normed 与子层 scratch 的 stream 依赖被证明后，
   再考虑 arena 别名。必须有布局、容量、对齐与所有权的可机读描述；
   不能用更换分配器掩盖生命周期，也不能直接宣称节省 320 MiB。

每一步必要构建后首先完整质量与五档 HTTP，全部通过才做逐位中间值、
边界与时间线核对。保持性能且净复杂度降低可接受，不预设吞吐收益。
过去分配合并实验曾回退，故不把移动 inject 与重排整个 arena 合成一次
实验，也不因逻辑等价免除 E2E。

## 当前边界

第一阶段已通过质量 11/11、五档性能 15/15、30 组逐位数值与 frame
生命周期检查；旧 Combine API 也参与对照，但不等于完整 MTP 验证。
性能范围重叠、容量未变。该合同是 R1 的起点，
R0/R1 完整机器可读计划、三计划调度与状态提交仍需独立完成。

## 首份可机读清单（提案）

[plans/grframe_main.json](plans/grframe_main.json) 记录一个主模型 GR 子层
的 buffer 形状、逻辑字节、所有权、读写节点、当前/提议顺序、数值舍入、
源码指纹与验收门禁。status=proposal_not_executable；没有 schema
验证器或执行器，也没有分配片上驻留或宣称峰值节省。第一阶段保留
现有两个 normed 区域，先隔离 inject 提前的影响。完整 D/P/S、状态提交
和 arena 别名仍未交付；这只是 R0 的一个可复核片段。

## 第一阶段实现

GatedResidualFrame 为非复制 RAII 对象，Read/Write 绑定同一 stream，
持有 residual 借用与小型 gate 分配，不保存 normed 指针。Write 消费后
释放，未完成子层的错误退出由析构清理。主 decoder 两个子层各有一帧。
PrepareInjectGate 统一新路径和旧 Combine 的数学；head/MTP 的公开
旧接口保留。Read/Write 明确防止未消费覆盖和重复消费。

两个 normed 工作区、预算与 carve 均未改。分配数量设计上不变，但
inject 分配与计算提前，pool 地址和生存时间可能改变；不能假定性能不变。
完整门禁后数值与时间线通过，详见 ../docs/GRFRAME_READWRITE_2026-09-21.md；
不代表完整执行器完成。

## 第二阶段已接受：单 normed 区域

基线 7a34dd3。两个 GRRead 复用 d_normed：前一次的 mix 与 inject
都排在下一次 Read 之前，Write 不持有该地址。DecoderLayerWorkspaceBytes
与 forward 的 scratch_bytes 同时从 3 个 hc_dim 区域减到 2 个，carve
删除一份 normed。d_combined 与 PLE trunk 因此前移，不能假定缓存或性能
完全不受影响。模型形状下每 token 减少 20480 字节的 decoder 预算。

ModelLoad 的 d_ws 实际申请取所有层和 head 的最大预算，见
src/model/model.cu；因此 decoder 预算差额不应直接描述成整机峰值下降。
本轮未新增 cudaMalloc/stream/event，也未把子层 workspace 相互别名。
构建零警告，首项质量 11/11、五档性能 15/15、门禁后容量与时间线
通过；实际编译预算的模型最大 workspace 减少 160 MiB，未测系统峰值。

后续 arena 别名须逐项核对：attn、MoE、PLE、HC GEMM scratch 与 normed
的最后消费者，尤其 src/quant/moe_gemm.cu 的辅助 stream 会在主 stream
上等待各自 event，不能只凭 host 返回判断可复用。一般性多模块合并不与
本轮混做；下一步先形成实际 offsets/capacity/alignment 的共享布局来源，
再用 E2E 验证物理布局变化。L1/L2 驻留不能由 LPDDR 地址别名推断。

## 下一步布局单源化（待实现，不并入当前候选）

当前 decoder 在三个位置分别表达同一布局：公开容量函数、forward 的
容量检查、forward 的指针 carve。下一阶段用一个轻量布局描述返回
各 region 的 offset/bytes 与 total_bytes，容量函数和 forward 共用它。
先保留当前区域顺序和所有偏移，不同时实施 arena 别名或减少分配。

| 区域 | 当前使用期 | 首次单源化保留的边界 |
|---|---|---|
| attention workspace | attention 子层 | 独立区域，含自身 GEMM scratch |
| MoE workspace | router 到 MoE combine | 独立区域，辅助 stream 必须汇合 |
| MoE GEMM scratch | routed/shared 投影 | 固定 32 MiB，与 HC scratch 分开 |
| HC GEMM scratch | 两个 GRRead | 固定 32 MiB，与 normed 不重叠 |
| PLE workspace | 可选 PLE 子层 | 独立区域，PLE trunk 存放在外部 |
| mixed / block_output | 子层输入/输出 | 两个独立的 T*hs*2 区域 |
| normed | 每个 GRRead 内 | 一份 T*hc*hs*2，依赖本轮验收 |
| combined | attention Write 至 MLP Write | 不与 MLP normed 或输出重叠 |
| PLE trunk | PLE 输出至 attention Write | 保留原始 residual 的借用期 |

模型的 hs=2560/hc=4 使激活区大小为 256 字节的整数倍。公开 API 仍
有 full==nullptr 的兼容预算分支，必须保留；不能将近似预算当成已有
精确 full-attention 形状。full->max_len 影响 T<=4 的 indexer scratch，
布局描述必须使用真实 full 参数，不能只由 T 推导所有容量。

接受标准除完整 HTTP 外，需在门禁后逐项对照旧/新 region offset、
容量和总字节，覆盖 T=1/3/4/5/33/257/8192、linear/full、PLE 与无 PLE。
同地址布局是净简化步骤；之后的物理别名必须作为另一项候选重做 E2E。
不新增解释器、动态 region 容器或每层 JSON 解析到热路径。
