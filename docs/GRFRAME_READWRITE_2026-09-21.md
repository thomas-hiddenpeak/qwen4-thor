# GRFrame read/write 第一阶段（2026-09-21）

状态：第一阶段接受。基线 fcb5925。构建零警告，首项质量 HTTP 11/11、
完整五档性能 15/15 通过；门禁后时间线与逐位对照通过。性能按持平理解，
正式参考保留 fcb5925，不以当前均值覆盖它。

## 本轮改变的依赖

原主层在 attention/MoE 结束后，用跨子层保留的 normed 做 inject
投影和 gate，再结合 residual 与子层输出。候选引入非复制
GatedResidualFrame：Read 调用原 mix，再提前生成 BF16 inject_gate；
Write 只读取借用的 residual、gate 和子层输出，不保存 normed 指针。

Read/Write 绑定同一 CUDA stream。Read 拒绝覆盖未消费帧；Write
要求 ready，提交 combine 后释放 gate 并复位。提前退出由析构在
所属 stream 上清理。stream 必须比 frame 活得更久，子层辅助 stream
必须沿既有依赖汇合后才能消费 block_output。

PrepareInjectGate 是旧 Combine 与新 Read 的公共数学实现：T=1
沿原 HC 专用 gated GEMV，其他 T 仍 BF16 GEMM 后单独 gate；投影
中间 BF16 舍入与 gate FP32/BF16 边界不变。head/MTP 保留旧 API。
调用处 SiLU 注释修正为实际 SiLU(down/hc)，计算本身不改动。

## 生命周期与复杂度边界

frame 拥有 T*4*2 字节 gate（T=8192 时 64 KiB），借用原 residual；
它不拥有序列状态、checkpoint 或模型权重。两个 normed 区域、workspace
预算与 carve 完全保留，第一阶段没有减少 arena 容量。gate 分配次数
设计上不变，但分配/计算提前会改变 pool 地址及与子层的生存重叠。

本轮增加 frame 类型与 ready/消费约束，仍保留旧 API；不能用代码行数
或类型封装宣称净复杂度已经下降。结构目标是移除主层 GRWrite 对
normed 的晚期依赖，为后续独立缩短/复用物理区域建立可检查边界。
没有证明峰值内存下降，也不预设吞吐提升；完整性能不回退仍是门禁。

## HTTP 证据

`.q4t-work/e2e/grframe-readwrite-20260921/` 保存旧/新二进制、三个
源文件快照、patch、构建日志与请求响应。质量 11/11，含 200K，输出
一致、服务退出 0。性能采用固定五档、各三次、单流、greedy、MTP
关闭、256 输出。全部五档 TTFT/decode 范围与基线重叠；输入输出
摘要一致、服务退出 0，按持平理解，不宣称稳定性能收益。


完整性能结果：

| 输入 token | TTFT 均值秒 | decode tok/s |
|---:|---:|---:|
| 1024 | 0.833337 | 18.581776 |
| 4096 | 2.795820 | 17.858984 |
| 8192 | 5.496244 | 18.081778 |
| 45056 | 31.858837 | 17.856518 |
| 204800 | 165.060732 | 17.055149 |

## 完整 E2E 后的验证

同一二进制先完成 4K HTTP 时间线，再运行 compare_grframe.py。工具
要求质量/完整性能、接受标记、二进制/源码/静态库指纹吻合；从
fcb5925 提取原 HC kernels、Mix、Combine，链接当前真实 frame API。
没有独立重写数学的 oracle，也不把当前旧 API 当作唯一基线。

实际覆盖 T=1/3/33/257/8192、三类有限 BF16 输入、combine 与 mixer
passthrough，共 30 组；比较 mixed/normed/最终输出与当前旧 Combine。
Read 后将候选 normed 填为无效位模式，再 Write，以检查最后消费者；
另检查 ready 重复 Read 拒绝、空/重复 Write 拒绝、消费后复用和提前退出
析构路径，含 T=0 no-op。清理路径执行不等于已证明不存在所有内存泄漏。
构建零警告、退出 0；30 组、2215864320 个有限 BF16 值逐位相同，
frame 状态转换检查通过，T=0 no-op 另行覆盖。

4K 时间线采集、服务退出与导出正常。对全部 96 个 prefill 与 24480 个
decode GR 子层检查：旧 gate 紧邻 Write，新 gate 紧随 Mix（prefill
中间保留原投影 GEMM），均移到子层之前。完整位置记录见
frame-order-comparison.json。

| 4K HTTP 时间线项 | 旧 | 新 |
|---|---:|---:|
| decode kernel 次数 | 442935 | 442935 |
| decode 异步分配/释放（各） | 141270 | 141270 |
| prefill kernel 次数 | 87703 | 87703 |
| prefill 异步分配/释放（各） | 554 | 554 |
| decode inject 累计 ms | 347.151424 | 344.905472 |
| decode combine 累计 ms | 47.249728 | 48.833856 |

profile 窗口 decode 14.477→14.462 秒、prefill 3.034→3.011 秒，带采集
开销，不能替代正式五档 E2E 或证明稳定收益。分配次数与 arena 容量均
未减少。head/MTP 旧 API 数值对照不替代完整 MTP、并发、多模态或全面
业务质量验证。

## 接受边界与下一步

接受的是 GRRead/GRWrite 的明确依赖边界：主层晚期 Write 不再消费
normed，正常路径和提前退出有显式 gate 所有权。新增类型与旧 API 并存
的成本仍在，不能称为净复杂度已下降或完整数据流引擎已交付。
下一阶段单独合并两个非重叠 normed 区域，同时修改预算和 carve；
仍先完整 HTTP，不能以本轮逐位相同代替新布局的验收。


候选二进制 SHA-256：`02ca2353126c3f8e811e2adf617f83174b9226fe7a674371db3a4b32521c290b`。
