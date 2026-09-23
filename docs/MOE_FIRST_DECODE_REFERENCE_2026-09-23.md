# 原4K请求首个decode前向的MoE参考

## 结论

完成48层首个decode前向的路由专家链路、router和共享支路参考。
首个decode前向消费的是prefill已生成的第一个token，不是负责
生成第一个token的prefill阶段。只覆盖该前向，未覆盖后续生成。
生产源码和接受二进制2392f6b1不变，原4K答案仍600440而非360284。

| 边界 | 比较数量 | 结果 |
|---|---:|---|
| router BF16投影 | 24576 | 同checkpoint FP64参考舍入；480个top-k相同 |
| gate/up输入FP4 | 1228800 | 实际尺度下独立重编码全部相同 |
| gate/up投影BF16 | 614400 | 同实际量化操作数的FP64参考舍入 |
| SwiGLU后FP4 | 307200 | 同设备重建和高精度独立SwiGLU量化 |
| down投影BF16 | 1228800 | 同实际量化操作数的FP64参考舍入 |
| 路由加权FP32 | 122880 | 同CPU逐槽fmaf参考 |
| 共享gate/up BF16 | 61440 | 8项不同于FP64参考舍入 |
| 共享SwiGLU BF16 | 30720 | 同CPU FP32参考；FP64参考有1项不同 |
| 共享down BF16 | 122880 | 14项不同于FP64参考舍入 |
| 从实际MoE输入连续重算共享MLP | 122880 | 140项不同，包含上游差异传播 |
| 最终共享合成BF16 | 122880 | 同FP64参考；CPU树归约/近似exp参考有3项不同 |

所有差异保留，不设新容忍比例，不宣称整模型正确。
共享合成的3项差异位于层20分量1405/2007及层28分量478，均相邻
BF16编码。CPU近似exp不是CUDA __expf的逐位模拟，尚未逐项隔离原因。
共享投影的22项差异也尚未独立重建GEMV归约解释。

## 实际decode路径与采集

源码T=1调用MoEDeviceDecode：PrepareMoEDecodeKernel填充设备端
专家矩阵/尺度/激活/输出指针和各组alpha/beta、形状数组，执行
两次grouped NVFP4 GEMM，再由CombineMoEDecodeKernel按十槽累加。
它没有prefill的专家token列表和分组行映射。router和共享BF16
投影在M=1走Bf16Gev单行路径，不能套用prefill cuBLAS重放结论。

新只读观测库零警告构建后，首项仍为tools/evalscope原4K无/有/无
观测HTTP。三次全文600440、4096输入/7输出、stop；三服务0，
质量驱动均1。观测不改变此请求输出，不等于质量通过或性能验收。

观测器先计数完整48层T=4096 router，再仅捕获接下来48层T=1。
启动阶段无观测输出；随后其他decode前向忽略。读回实际DeviceBatch，
检查形状、beta=0及专家权重/尺度指针对应StageWeights偏移，读取
它指向的实际量化激活和权重；并非仅根据专家id从checkpoint猜值。
这次没有直接拦截grouped cuBLAS描述符或逐项验证alpha指针数组；
保存的batch alpha与checkpoint乘积相同，实际结果另与参考比对。

480个设备槽与router专家id对应，gate/up到SwiGLU输入相同，down
输出到对应合成槽相同。实际权重/尺度/源输入及alpha均匹配逐层、
逐专家checkpoint合同。共享gate向量也逐层匹配，48份身份摘要互异。

## 格式错误与独立算术边界

76800组GU输入尺度设备重建确认20次高值错误，来自层2及44的
两个输入分组，各被十专家重复使用。19200组down输入尺度确认
63组高值错误，涉及25层。全部落在[248,432)提前饱和区，CUDA原生
编码同独立最近偶数枚举；旧编码全部同接受版真实捕获。
这说明缺陷也在首decode前向触发，不证明它造成原错题答案。

按实际尺度进行FP4编码和FP64投影全部位同。设备SwiGLU与FP64
数学值最大绝对差约1.04988e-6；高精度SwiGLU经FP32舍入并按
实际尺度量化后307200个FP4码仍相同。仍未按修正尺度传播模型，
没有复测或追认已拒绝的格式修正候选。

离线复用已编译的接受版快照重建程序，摘要绑定相同。首次复制
inter重建可执行文件漏保留执行权限，启动退出126；错误日志保留，
补执行位后同一二进制退出0，未改变代码/输入或重跑HTTP。
GU尺度重建退出0。所有CPU分析均完成，原始结果及差异索引保留。

## 范围和下一步

所有层仍从实际MoE输入开始，没有独立计算上游attention/GDN/PLE/
残差或跨层传播状态。只覆盖一个请求的一个decode前向、单流、
MTP关闭；不能据此声明后续token、其他上下文、并发或整模型正确。
下一步核对decode共享GEMV实际权重及归约，解释新增共享差异；
同时明确上游状态参考缺口，避免把局部一致误当完整正确性确认。

证据：`.q4t-work/e2e/moe-first-decode-reference-20260923/`包含三侧
HTTP、实际快照、gu/down/shared/router参考、combine-analysis.json、
两种尺度重建、merge-differences.json和artifact-binding.json。
prepared同名目录保留构建、控制器、分析脚本、重用二进制来源与摘要。
仓库保存首decode观测及合成参考模板，其他算术工具与上一阶段同构。
