# Decoder workspace 资源合同候选

日期：2026-09-21。父版 81cdbf8，固定性能参考 fcb5925。
状态：已接受；完整 HTTP、链接合同与 4K 时间线均通过。

## 实现

DecoderWorkspaceLayout 从私有实现移到 decoder_workspace.h；原构造
函数仍由公开容量查询和 forward 共用。新增 hidden_bytes/hyper_bytes
保存构造时已计算的逻辑大小，不另写一套容量公式。内存位置、总预算和
forward 调用顺序预期不变，仍须通过真实 HTTP 和链接布局核对。

DescribeDecoderWorkspace 从实际布局导出 13 个视图，含 name、offset、
bytes、live_phases。7 个粗粒度阶段为 PLE、attention Read、attention、
attention Write、MLP Read、MoE、MLP Write。描述函数不在 forward
热路径调用，没有 JSON 解析或新增内存分配。

存活掩码表示整个阶段需要保留该区域，不是 kernel 启动时刻，也不是
精确读写计数。例如 gate 跨两组 Read/子层/Write，combined 从 attention
Write 保持到 MLP Write；normed/down/up 仅在两次 Read 内存活，允许
与后续 MoE 暂存共享地址。辅助 stream 仍须遵循现有汇合依赖。

## 验收范围

证据目录 `.q4t-work/e2e/decoder-resource-contract-20260921/`。
候选 SHA-256：`49b3238df4cc06dc3a3052de5ff5f7a5006dbbc5740cd46404d9b7c26b93cb04`。

- 必要构建后第一项测试为 tools/evalscope 真实 HTTP，质量 11/11、
  输出一致、服务退出 0；完整性能 15/15 与双参考范围审查通过。
- 门禁后检查器链接 HTTP 候选实际库，覆盖 252 布局与 504 次容量查询，
  将父版的 20 个布局字段逐项对照，检查每个资源视图的绑定。
- 检查地址对齐、容量、同阶段重叠，并要求 forward 实现文本与父版一致。
  五类错误描述各在每种布局中注入，预期 1260 个拒绝。
- 导出的 resource-views.json 来源为实际链接函数，不是文档重算值。
- 4K HTTP 时间线核对 kernel 和分配计数，采集时间不替代正式 E2E 指标。

检查器已在完整 HTTP 门禁后编译执行通过，构建零警告。存活阶段是根据当前代码审查制定的
语义合同，不能仅凭掩码检查证明实际 GPU 调度符合任意未来修改。
当前工具还要求 forward 文本保持一致；未来改顺序需重审合同与证据。

## 未覆盖边界

只覆盖 decoder workspace。模型权重、KV/indexer、SSM、PLE 的外部
状态、I/O buffer、错误恢复和序列提交尚未纳入此描述。LPDDR 地址和
容量不保证 L1/L2 驻留，子层内部寄存器/shared memory 与带宽预算仍待
相应映射。它不是通用 schema、自动内存规划器或 D/P/S 执行器。

本轮没有吞吐或容量改善目标；目的在于让已经落实的资源布局能够直接
导出和检查，减少后续优化依赖手工解释的部分。

## 本轮结果

| 输入 token | TTFT 均值秒 | decode tok/s |
|---:|---:|---:|
| 1024 | 0.831568 | 18.588227 |
| 4096 | 2.784152 | 17.908853 |
| 8192 | 5.482405 | 18.128563 |
| 45056 | 31.751562 | 17.884595 |
| 204800 | 164.876270 | 17.090991 |

TTFT 不是纯 prefill。相对父版和固定参考均无不利重复范围分离，
性能按持平理解，不因小样本差异更新正式参考或宣称稳定提速。

合同检查实际通过 252 布局、各 20 个旧字段、504 次公开容量，
13 个视图逐项绑定；1260 个错误描述全部拒绝。forward 实现文本
与父版完全相同。resource-views.json 保存了 252 组、3276 个视图，
地址和大小来自实际链接函数；当前没有新增容量或布局变化。

## 时间线与接受决定

4K prefill 的 87703 个 kernel、decode 的 442935 个 kernel，逐 kernel
调用数与父版相同；异步分配/释放分别保持各 266 次与各 67830 次，
对应 decode 每步各 266 次。计数 D2H 保持 prefill=48/decode=0。
profile 窗口 prefill 3018.383→3029.165 ms，decode
14412.884→14406.733 ms；采集扰动不用于宣称正式性能变化。

保留这一资源可检查性边界，正式参考仍为 fcb5925。本轮没有容量或
吞吐提升主张，不等同于 R0 全模型资源账或 D/P/S 执行器完成。
下一步按 R1 路线评估 GRWrite→下一 GRRead 衔接，先审查真实数值
顺序、BF16 舍入和 residual 最后消费者，禁止以融合减少 kernel 数
代替完整 E2E 与精度验收。
