# 固定 k3 suffix：同步报告完成合同与有界验证

本阶段修复采集完成确认，推理程序、suffix 准入及两种 head 均不改。
完整交付目标仍待采集完整性、子集 200K/8K 归因及性能保护闭合；研究
候选不替换默认程序。旧自动回调阶段 ENV_ABORT 保留，不追认成功。

## 已固定依据与实施范围

固定 Nsight 2026.5.1.161 的 start-help 与随包 UserGuide 明确支持
`--capture-range-end=repeat:1:sync`：报告生成期间阻塞应用线程。
同一实际目标已绑定 PID/start_ticks 的 `cudaProfilerStop` 成功返回收据
因此是本次同步范围完成凭据。fixture-ready 和模型 exec-capture end
均在该调用返回后输出，现有程序无需重编译。

新模式独立于旧 manual/automatic 模式；禁止额外手动 stop 与回调回退。
先确认实际目标存活与成功收据，再从本会话 server.log 的唯一 Generated
报告绑定所属目录中的唯一普通非空报告文件，按实际名称导出，不猜范围
编号。Generated 只绑定路径；文件存在、shutdown 或 launcher 退出均
不替代同步调用完成、实际目标独立退出或 trace 完整性。

全部实际目标/未知进程 warning、实际丢失、memory activity 覆盖门保持。
原始 NVLOG 终态证据继续记录。无计数参数的成功 memset 缺活动不视为零
字节，也不把已记录 GPU union 的间隙称为真实空闲。

旧 SQLite 的 714727 条目标 API correlationId 唯一，266389 kernel、4551
copy、7902 memset 全部唯一关联 API。6381 条成功 cudaMemsetAsync 无任何
活动，并非现有 SQLite 的简单 runtime/driver join 错误；原库保持不变。

CUPTI 13.3 已公开多 subscriber V2 支持，因此“所有环境不能与 Nsight
并用”的说法过强。本机驱动是 r595_00，而该机制要求 r610+；实际 Nsight
NVLOG 还明确调用 V1 single-subscriber。当前组合不可用该路线。后续若
需要参数 probe，须独立进程、固定真实算法和 workspace，不与当前 Nsight
同时订阅，也不能倒推旧 6381 条全部合法无工作。

## 冻结预算和判定

`.q4t-work/suffix-sync-stop-20261026/scope.json` 事前冻结：7200 秒工作墙钟；
CPU 总计至多 64 方法/180 秒，一次 suite，只有记录具体失败并修复才允许
一次定向修复 suite，仍共用总时间。一次完整 GPU 共享独占锁 4800 秒；
直接/包装控制各一次、合计 600 秒（单控制 300 秒）。仅控制通过后按
8K off、8K on、200K on、200K off 顺序执行，最多 4 加载/6 HTTP 请求。
200K 前置 8K 顺序、256 输出、输入与缓存合同继承原冻结计划。零构建、
零推理修改、零重试、零选项或版本扫描。竞争、监控或自有清理失败记
ENV_ABORT。任一完整性门未通过停止依赖分析；不自动扩采集。

继承未变 monitor CPU/native64、fixture/analyzer、有限正确性与两 head
重复收益证据；完整 head +13.6774%/+14.4034%，子集共享 B 描述性 GM
+6.5335%/+6.7304%。这些不抵销既有五档保护 NO_GO，也不是新采样。
默认 f356、发布 sidecar 与子集产物身份继续保护。工具资格不代替五档
HTTP 性能验收，profile 耗时不作正常吞吐样本。

## 结果

62 项 CPU 最终全部通过（runner19/manual16/automatic14/synchronous13）。
首 suite 仅一个旧最小 mock 缺少 nsys 配置发生 error；补入现有 manual
计划后，按冻结规则执行唯一修复 suite，零失败/错误/跳过，两次总计
1.149678 秒。推理实现和断言未放宽，406 源前后 SHA 一致，失败记录保留。
唯一完整持锁阶段执行 53.068531 秒，首 direct 控制进入真实同步模式，
32 步输出及 APIStop0 收据到达，报告生成/导入并回到 Launched。实际
`trace.1.nsys-rep` 已生成，但本地等待 Generated 文本的路径门 45.000178
秒无绑定收据，随后只清理自有目标；shutdown 时会话已因目标退出结束。
阶段为 ENV_ABORT，0 模型/0 HTTP/0 导出；wrapper 与 200K/8K 均未执行。

这是本地过强的路径绑定依赖，不是新的监控权限失败。最终日志已有
Generated，最后写入 mtime 落在清理期间；没有逐行时间戳，首次打印或
flush 的精确时刻仍 UNKNOWN。同步完成与 stdout 输出时点被错误耦合。
不能因此宣布 warning 误报、事件齐全或性能下降修复。

监控 54/54 写入、336 次合法进程观测、pending0、无缺 io 或监控失败。
独立审计 16.182492 秒，46626 冻结文件、21297 父 snapshot、33 raw 及
197 模型元数据一致，7 自有身份 ABSENT、PGID 无残留、共享 lease 释放。
审计前静态修正一次 CPU exit 收据字段识别，未消费额外审计运行。

| 维度 | 决定 |
|---|---|
| 证据身份完整性 | PASS；失败现场有效，未追认为成功 |
| 环境/阶段有效性 | ENV_ABORT，原因是超时后的强制自有清理 |
| 测量完整性 | false；全部模型对照缺失 |
| 统计结论 | NOT_EVALUATED_DIAGNOSTIC_ONLY，无新性能结论 |
| 工程决定 | NOT_ACCEPTED_TOOL_VALIDATION_ABORT；保持现有交付 |

完整目标继续 active。本轮不追加 CPU suite、采集或参数试验。后续具体
修复只取消 Generated 及时可见这一前提：在同实际目标同步返回收据后，
绑定本次初始无报告的独有规范目录中唯一匹配输出前缀的普通非空报告，
核对 FD/lstat、inode/大小/mtime 与 SHA；若 Generated 出现但与路径矛盾
仍拒绝。全部 warning、memory、负控、独立实际目标退出门保持。

原始阶段 `.q4t-work/suffix-sync-stop-20261026/diagnostic-v1`、父归档链及
默认程序/sidecar/子集身份保留。公开核验见
[摘要](evidence/mtp-suffix-synchronous-20261026/summary.json)、
[独立审计](evidence/mtp-suffix-synchronous-20261026/audit.json)、
[六源法证](evidence/mtp-suffix-synchronous-20261026/sync-forensics.json)。

增量封存 926 条目/57447505 字节，独立 inode 与逐文件 SHA 回读通过，seal
`2d3ff84c5f00541943a1d2af31dd1263dbe620ae1c6770fb35434e589d5bab97`。
含两轮 CPU 源快照、raw/lease/overlay/审计/法证；大父快照及工具包通过
既有三层归档锚点复用，非自包含，原路径继续保留。此次研究提交和归档
不表示工具资格或 suffix 性能验收通过，无部署或新增健康检查 HTTP。
