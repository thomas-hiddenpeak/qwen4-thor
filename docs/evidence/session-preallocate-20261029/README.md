# 文件预分配候选证据

本阶段结论为 **NO_GO**；证据有效、测量完整，性能条件未全部达到。
详见[结果与停止决定](../../SESSION_PREALLOCATE_RESULT_2026-10-29.md)和
[冻结方案](../../SESSION_PREALLOCATE_PLAN_2026-10-29.md)。

此目录保留原文件的逐字节副本：冻结身份/预算、最终决定、资源摘要、
默认版本保留和依赖复用核对、全部四个服务的日志/结果/恢复/审计、
CPU合同输出、构建输出、监控退出和归档收据。`files.json`记录各副本
的SHA256和字节数，不含其自身与本说明。

完整原始HTTP数据库、请求响应、逐秒遥测、程序、输入和绑定源码均保留
于仓库本地`.q4t-work/session-preallocate-20261029-archive/`，1362个文件
已经独立逐项校验。`archive-receipt.json`给出归档manifest身份。
未用日志大小裁掉证据；版本库仅选取便于审阅的副本，完整证据仍可追溯。

`run/state.json`原状态为`MEASURED_REQUIRES_REVIEW`，
`lease/exit.json`为`COMPLETED`；离线工程结论在`decision.json`中为
`NO_GO`。这表示验证正常完成但候选性能未达标，不是监控失败或ENV_ABORT。
该目录不包含新的推理测试，也不修改任何冻结结果。
