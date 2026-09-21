# 从 runner 演进 GRFrame 的第一条执行合同

2026-09-21。静态方案，未实现；先完成短路径 top-k 当前验收。

## 已核对的调用链

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
调用方拥有的 BF16 inject_gate[T,4]、T/形状及有效性，不拥有 normed。
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

当前只静态核对 src/model/decoder_layer.cu 与 hyperconnection.cu 的
正常执行消费者；head 与 MTP 的接口兼容性已发现但未完成新实现验证。
未运行 GRFrame 原型、没有新的容量或性能实测。该合同是 R1 的起点，
R0/R1 完整机器可读计划、三计划调度与状态提交仍需独立完成。

## 首份可机读清单（提案）

[plans/grframe_main.json](plans/grframe_main.json) 记录一个主模型 GR 子层
的 buffer 形状、逻辑字节、所有权、读写节点、当前/提议顺序、数值舍入、
源码指纹与验收门禁。status=proposal_not_executable；没有 schema
验证器或执行器，也没有分配片上驻留或宣称峰值节省。第一阶段保留
现有两个 normed 区域，先隔离 inject 提前的影响。完整 D/P/S、状态提交
和 arena 别名仍未交付；这只是 R0 的一个可复核片段。
