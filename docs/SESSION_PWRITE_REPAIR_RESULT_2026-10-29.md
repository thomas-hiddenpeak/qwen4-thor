# pwrite 采集修复与有界 HTTP 观测结果

2026-10-29，工程决定 **STOP_ENV_ABORT_NOT_DELIVERED**。次级 NVMe
缺口机制已在 CPU 夹具复现并核对；完整 head 的慢保存主要增量落在
主机可见的块请求 issue→NVMe CQ 完成处理区间。两种 head 的 20 HTTP
未完成：完整 head 10 条及 2 次覆盖后恢复通过，子集首请求遇到采集
stack helper 错误而取消。本轮不扩样、不安装，原发布 NO_GO 保持。

## 范围、身份和执行

[冻结方案](SESSION_PWRITE_REPAIR_PLAN_2026-10-29.md)为新 90 分钟阶段，
从 06:51:07 UTC 起到 08:21:07；原阶段预算没有续写。实际两 CPU
存储夹具共 50,872,320 字节（48.516 MiB），没有新 GPU 数值夹具。
CPU 关联测试首组因 JSON 键类型断言失败，修正后 28 项通过；覆盖文件
代次、fd/CID/request 复用、partial completion、requeue、并发交错及
精确字节闭合。原失败保留。所有推理均由整阶段 GPU 独占驱动持锁。

- 受测程序仍为 46958b6c538d4410bea204bce1437c433b7a1ec87ea02855fb9417bece1ae9a6；
  PREALLOCATE=0、SAVE_TIMING=1，四槽 208896＋65536×3，suffix/两缓存开启。
- 默认程序仍为 6f3722b26e5b9d1b08d405e873d1f90d72f4b5c1aec2dd14589d1e27390b7965；
  发布元数据仍为 5fdca879f84da6795b66113833de7167df6edbcff0d97e09d474113e47a67de2。
- 一次 64 GiB 预处理后因缺少 smartctl 停止（0 模型/HTTP），改用已安装
  nvme SMART；第二次因 sudo 根父进程监控 EACCES 停止（0 模型/HTTP）。
  修为普通用户 subreaper 接管降权 collector，CPU 权限/退出检查通过。
  不重复预处理，原 HTTP 截止 08:00:50.406 不变。
- 最终阶段 07:41:08.451～07:53:19.386，2 次模型启动，11 条准入，
  10 成功＋1 取消。完整 head 的输出 oracle、恢复 131323 token、健康
  与容量检查通过。子集没有完成请求/恢复结论。驱动 ENV_ABORT、外层
  guard COMMAND_FAILED 两种原状态均保留，未伪造最终监控退出屏障。

预算偏差明确保留：CPU ownership 检查在工具截止前完成，但最终门禁
元数据晚了 **0.800317 秒**，`within_tool_deadline=false`；因此不是严格
无偏差的工具准入。HTTP 具体失败后额外执行 5 项纯 CPU 失败路径检查，
超出初始两组 CPU 次数上限；没有追加模型、HTTP、直接写夹具或延长总截止。

## 次级事件缺口的原因

旧记录 raw completion 265、formatted 259 的 6 个缺口不能凭空补事件。
新夹具分别观察到 raw/formatted 为 270/246、137/130；formatted
complete 与 CQ 的缺口各为 24、7，分别精确等于内核 program info 的
recursion_misses。raw 中断标记还直接观察到普通 BPF 正在运行。
这支持 Linux 普通 tracepoint 的 per-CPU 嵌套执行保护导致次级回调跳过，
不是设备少完成 I/O。旧 6 条缺口的机制为证据支持的推断，不声称旧计数
已经实测。机制参考 [Linux v6.8 bpf_trace.c](https://raw.githubusercontent.com/torvalds/linux/v6.8/kernel/trace/bpf_trace.c)
及 [perf 调用路径](https://raw.githubusercontent.com/torvalds/linux/v6.8/kernel/events/core.c)；
本机 6.8.12-tegra 的新夹具计数是直接证据。

关联主链使用 raw request/CID 代次和逐字节闭合；普通次级缺失单独报告。
不把没有 insert 的直接派发请求当作已测得零排队；不把缺 wakeup 的 CPU
排队记为零。HTTP 已落盘前缀包含 245 个缺 wakeup 区间，其完整 CPU
唤醒排队值为 unknown，只有已知下界。

## 完整 head 的有效慢写窗口

下表各主快照约 4.160 GB、598 次 pwrite；每个文件身份、偏移、字节及
调用数独立与服务端分段日志核对。单位秒；区间列是并集，列间会重叠，
**不能相加**。恢复后的两个文件比初始文件大约 0.0205%。

| 请求 | pwrite 日志 | issue→CQ 并集 | ext4 map 并集 | 已知 insert→issue 并集 |
|---|---:|---:|---:|---:|
| auto-0 初始主会话 | 2.095 | 2.024 | 0.0325 | 0.00071 |
| auto-4 公共 ON 主会话 | 3.712 | 3.648 | 0.0284 | 0.00122 |
| auto-8 恢复主会话 | 16.697 | 16.641 | 0.0966 | 0.00060 |
| auto-9 恢复主会话 | 13.582 | 13.515 | 0.0675 | 0.00058 |

慢两窗的 issue→CQ 覆盖约 99.67%/99.52% 的 syscall 墙钟，ext4 map
大部分与 I/O 重叠；CQ→raw completion 并集约 394 ms，快慢窗接近。
这把十秒级增量定位到主机可见的在途 I/O 等待区间，不能进一步认定
SSD 内部 GC、NAND 写入或硬件故障。issue 早于真正 doorbell，CQ 事件
晚于硬件完成，还包含设备队列、驱动/PCIe/IRQ 延迟。完整调度事件及
栈表缺失，不能声称彻底分离设备内部耗时和所有 CPU 延迟。

## 实际失败、修复及证据限制

采集计数 `[1,1817733,0,0,1,0,225]` 表明 stack helper 失败 1 次、ring
lost/map/read error 为 0。已落盘 1,794,997 条、229,759,616 字节；至少
22,736 条尾部记录未持久化，失败事件不在落盘部分，具体 errno **未知**。
原异常出口还漏存栈表、program counters 和 collector I/O，不能补造。

修复异常出口为 detach→drain→保存尾部→保存辅助表，保留 FAILED；同步
失败不重复追加已写字节。另允许合法零字节 FLUSH，WRITE 仍拒绝零字节，
缺 wakeup 保留 unknown。5 项针对性 CPU 检查通过，包括真实落盘前缀
4,320 个完整 pwrite、209,818 个 request 生命周期回放。当前修复版本
没有再做 GPU 验证；stack helper 本身错误仍待取得 errno 后定位，不能
称采集工具已全面通过。实际 HTTP 工具源码另存 frozen-tools，和事后
修复源码分开绑定，失败全局状态不被局部离线复核覆盖。

731 个资源样本全部落盘，资源监控本身无失败；collector 峰值 RSS
102,100,992 字节，maps 21,703,536 字节，加 4 MiB 输出缓存的保守计数
127,998,832 字节（可能重计共享项，不是精确独占物理 RAM）；模型及
collector swap 为 0，最低 MemAvailable 10,153,533,440 字节。系统页缓存
已在可用内存包络内，未取得完整 trace I/O 开销分解。SMART 前后介质
错误/严重告警均 0，温度 312→322 K，不能据此证明没有设备延迟。
观察期间做过三次离线前缀读取，自身观测 I/O 未完整量化；结果为有扰动
条件下的诊断窗口，不用于净性能或设备内部因果宣称。

## 决定、复用与证据

证据有效性：完整 head 闭合文件窗口部分有效；测量完整性：10/20 HTTP、
2/4 恢复；统计结论：无优化或统计非劣结论；工程决定：ENV_ABORT 后停止，
保持默认及原 NO_GO。旧 A112/S32、预分配阶段 40HTTP/8恢复/68CPU 的
未变运行时合同直接复用，不重新跑整套验收。

工作证据 `.q4t-work/session-pwrite-repair-20261029/`；独立完整归档与
SHA 清单见 [版本化证据入口](evidence/session-pwrite-repair-20261029/README.md)。
保留所有原始事件、三次启动状态、错误日志、实际受测源码及事后修复；
文件大小不作为否定有效闭合窗口的理由。下一步若重新授权，只针对
stack helper errno/失败收尾实机验证及缺失的子集观测，不自动扩样。
