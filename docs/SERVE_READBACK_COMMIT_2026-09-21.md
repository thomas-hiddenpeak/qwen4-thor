# Serve 结果读回与发布边界

2026-09-21，父版 ede4e09，固定性能参考 fcb5925。本阶段已接受，
零警告构建后首项质量 HTTP 11/11 通过，完整性能及双参考范围门禁通过；故障验证与正常路径时间线通过，按性能持平接受。

## 源码核对

ModelSequence 的 position/history 是 host 元数据，底层 RunPrefill、
ModelDecodeStep 和批量 forward 排队 GPU 工作；模型函数返回成功不
等价于 GPU 工作已完成。serve 最终通过 D2H 与 stream 同步取得结果。

原 chat_server.cpp 的五个读回位置忽略复制和同步返回值：

- 调度器单请求 prefill：tmp 序列 forward 后读取最后一行 logits。
- 调度器批量 prefill：逐序列读取最后行，统一同步。
- 调度器普通 decode：GPU argmax 后读回 token，按 s.ok 发布结果。
- 内联 prefill：包含长 prompt 分块路径，读取最后 chunk 的最后行。
- 内联普通 decode：读 logits 后直接 CPU argmax。

因此即使结果未能成功读回，s 仍可能成功，调度器发布旧 host token，
请求线程随后推进 position/history。prefill 另有一次同步诊断，但
原先只打印错误，没有让请求失败。这是错误路径的代码事实，不代表
已有正常 E2E 观察到 GPU 故障或证明历史输出受到影响。

另一个独立缺口是 ModelSequence 的 seq_id 与 Prefill/DecodeStepSeq
单独参数同时存在，实际调用主要显式传入；本阶段不调整该 API。
分块 prefill 的 host 位置仍在整个 prompt 完成后修正，不是完整的
逐 chunk 状态提交。MTP 的提交/恢复和多设备 PD 也不在本阶段实现。

## 候选修复

FinishHostReadback 在原位置等待同一 default stream，返回复制或
同步的首个错误并标记 gpu_healthy=false。即使复制失败也完成原来
的同步边界；多序列复制仍全部排队、只同步一次，不按行新增同步。
调度器把错误送入已有失败通知路径，不发布 token；prefill 走 HTTP
500；内联 decode 在 argmax 前终止，不消费旧 host logits。
prefill 额外同步诊断保留原位置，但检测到错误后也令请求失败。

没有增加正常路径的 CUDA 调用或改变 kernel、精度、布局。普通
streaming decode 已发出的内容无法撤回，仍沿用现有 stop/清理语义，
没有新增 SSE 错误协议。内联模型 API 可能已推进 host 元数据，但
失败后结束整个序列；不是回滚后继续，也不是跨线程事务。

## 验收顺序与待验证

证据目录 `.q4t-work/e2e/serve-readback-commit-20260921/`。
先质量 11 题和五档各三次 HTTP，同输入/输出对父版及固定参考；
若性能出现不利范围分离，先相邻旧→新→旧复核，不进行专项。
完整门禁后才验证复制失败、同步失败不会发布旧结果，以及正常
路径 CUDA 调用次数。需要分别覆盖 prefill 和 decode 的发布边界，
不能用单独 helper 的成功分支测试冒充完整服务错误路径通过。
错误注入仅用于独立验证进程，不能放进正式服务或污染性能结果。

候选启动时未执行故障注入或 profile；最终验证结果见下文。
正常 E2E 通过本身不能证明错误路径。

## 首项质量 HTTP

11/11 请求与父版输出一致，服务退出 0，failure=null。
二进制 SHA-256 为
`5871dcbcb3d965a468d21b461deba19d2a2d7605b16f5543ae605da137029a6e`。
候选源码与冻结副本一致。已串行启动完整五档性能；尚未运行任何
故障注入或 profile，不以正常质量题通过宣称错误路径验证完成。

## 完整 HTTP 门禁

质量 11/11、五档 HTTP/输出 15/15，服务退出 0；输入与输出摘要
一致。直接父版 ede4e09 与固定参考 fcb5925 的 TTFT/decode 均无
不利范围分离，不需要相邻复测。微小均值变化不作为提速结论。

| 输入 token | TTFT 秒 | decode tok/s |
|---:|---:|---:|
| 1024 | 0.809265 | 18.594904 |
| 4096 | 2.702420 | 17.855724 |
| 8192 | 5.284269 | 18.058373 |
| 45056 | 30.649916 | 17.855617 |
| 204800 | 159.645265 | 17.079701 |

门禁后启动独立 HTTP 故障验证工具 verify_readback_http.py：通过仅在
验证进程使用的 LD_PRELOAD 替身，对指定 D2H 结果复制或随后的同步
返回一次错误。同步替身先完成真正同步，不制造真实 GPU 故障。
父版/候选 × 调度 prefill/内联 prefill/调度 decode × copy/sync，共
12 组。检查注入次数、首请求、健康状态及后续请求拒绝；不以 helper
测试替代实际服务流程。当前仍在运行，不能预先计作通过。

## 最终验收

12/12 独立进程用例通过，全部服务正常退出。父版六组均在注入
copy/sync 错误后仍返回 200、健康标记为 true、接受后续请求。
候选四组 prefill 用例返回 500；两组 decode 用例只返回故障前
已产生的 1 个 token。候选六组健康检查均 503/false，后续请求均
503，未继续发布旧结果。记录见 fault-http/*/responses.json。

正常 4K profiled HTTP 输出与正式矩阵一致，采集器和服务退出 0。
与父版相比，prefill/decode 的全部 kernel 名称及次数相同，分别
87655/430695 次；采集窗口内全部 CUDA API 次数也相同，包含
memcpy 101/1276、stream synchronize 48/256、异步分配与释放
各 266/67830 次。decode 窗口包含 255 次 forward。
窗口耗时 2913.429→2889.755 ms、14560.942→14532.565 ms；
单次插桩结果不作为提速证明。源码、库及三个 HTTP 阶段的二进制
摘要一致，见 final-audit.json。

按错误路径修复、正常性能持平接受。限制：故障验证是非流式单请求，
未强制多请求组批、未覆盖内联 decode 回退路径；这些路径有源码
审查但不能等同于运行覆盖。未模拟真实设备损坏、进程崩溃或 MTP
恢复。streaming 失败仍使用既有 stop 结束方式，没有新增错误协议。
固定参考 fcb5925 不变，完整模型专用执行器和状态事务仍未完成。
